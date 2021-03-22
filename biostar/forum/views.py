import logging
from datetime import timedelta
from functools import wraps, lru_cache
import os
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import ensure_csrf_cookie
from django.db.models import Count, Q
from django.core.paginator import Paginator
from django.shortcuts import render, redirect, reverse
from django.core.cache import cache
from ratelimit.decorators import ratelimit
from taggit.models import Tag

from biostar.accounts.models import Profile
from biostar.forum import forms, auth, tasks, util, search, models
from biostar.forum.const import *
from biostar.forum.models import Post, Vote, Badge, Subscription, Award, Log
from biostar.utils.decorators import is_moderator

from biostar.forum.auth import db_logger

User = get_user_model()

logger = logging.getLogger('engine')

RATELIMIT_KEY = settings.RATELIMIT_KEY

# Valid post values as they correspond to database post types.
POST_TYPE = dict(
    question=Post.QUESTION,
    jobs=Post.JOB,
    tutorial=Post.TUTORIAL,
    forum=Post.FORUM,
    planet=Post.BLOG,
    tools=Post.TOOL,
    news=Post.NEWS,
    pages=Post.PAGE,
)

LIMIT_MAP = dict(
    all=0,
    today=1,
    week=7,
    month=30,
    year=365
)

def post_exists(func):
    """
    Ensure uid passed to view function exists.
    """
    @wraps(func)
    def _wrapper_(request, **kwargs):
        uid = kwargs.get('uid')
        post = Post.objects.filter(uid=uid).exists()
        if not post:
            messages.error(request, "Post does not exist.")
            return redirect(reverse("post_list"))
        return func(request, **kwargs)
    return _wrapper_


class CachedPaginator(Paginator):
    """
    Paginator that caches the count call.
    """

    # Time to live for the cache, in seconds
    TTL = 3000

    def __init__(self, cache_key='', ttl=None, msg='', *args, **kwargs):
        self.cache_key = cache_key
        self.ttl = ttl or self.TTL
        self.msg = msg
        super(CachedPaginator, self).__init__(*args, **kwargs)

    @property
    def count(self):

        # Start timer.
        start = time.time()

        # if self.cache_key:
        #     value = cache.get(self.cache_key) or super(CachedPaginator, self).count
        #     cache.add(self.cache_key, value, self.ttl)
        # else:
        #     #value = super(CachedPaginator, self).count
        value = 1000 # testing out a fix

        delta = int((time.time() - start) * 1000)

        msg = f'time={delta}ms count: {self.msg}'

        if delta > 1000:
            logger.warning(f"SLOW: {msg}")
        elif settings.DEBUG:
            logger.info(f'{msg}')

        return value


def get_posts(user, topic="", order="", limit=None):
    """
    Generates a post list on a topic.
    """
    # Topics are case insensitive.
    topic = topic or LATEST
    topic = topic.lower()

    # Detect known post types.
    post_type = POST_TYPE.get(topic)
    query = Post.objects.valid_posts(u=user, is_toplevel=True)

    # Determines how to start the more_like_this.
    if post_type:
        query = query.filter(type=post_type)

    elif topic == SHOW_SPAM:
        query = query.filter(Q(spam=Post.SPAM))

    elif topic == OPEN:
        query = query.filter(type=Post.QUESTION, answer_count=0)

    elif topic == BOOKMARKS and user.is_authenticated:
        query = Post.objects.valid_posts(u=user, votes__author=user, votes__type=Vote.BOOKMARK)

    elif topic == FOLLOWING and user.is_authenticated:
        query = query.filter(subs__user=user).exclude(subs__type=Subscription.NO_MESSAGES)

    elif topic == MYPOSTS and user.is_authenticated:
        # Show users all of their posts ( deleted, spam, or quarantined )
        query = Post.objects.filter(author=user)

    elif topic == MYVOTES and user.is_authenticated:
        query = query.filter(votes__post__author=user)

    elif topic == MYTAGS and user.is_authenticated:
        tags = map(lambda t: t.lower(), user.profile.my_tags.split(","))
        query = query.filter(tags__name__in=tags).distinct()

    # Search for tags
    elif topic != LATEST and (topic not in POST_TYPE):
        query = query.filter(tags__name=topic.lower())
    else:
        # Exclude spam posts unless specifically on the tab.
        query = query.exclude(Q(spam=Post.SPAM))

    # Apply post ordering.
    if ORDER_MAPPER.get(order):
        ordering = ORDER_MAPPER.get(order)
        query = query.order_by(ordering)
    else:
        query = query.order_by("-rank")

    days = LIMIT_MAP.get(limit, 0)
    # Apply time limit if required.
    if days:
        delta = util.now() - timedelta(days=days)
        query = query.filter(lastedit_date__gt=delta)

    # Select related information used during rendering.
    query = query.select_related("root").select_related("author__profile", "lastedit_user__profile")

    return query


def post_search(request):

    query = request.GET.get('query', '')
    length = len(query.replace(" ", ""))

    if length < settings.SEARCH_CHAR_MIN:
        messages.error(request, "Enter more characters before preforming search.")
        return redirect(reverse('post_list'))

    results = search.perform_search(query=query)

    total = len(results)
    template_name = "search/search_results.html"

    question_flag = Post.QUESTION
    context = dict(results=results, query=query, total=total, template_name=template_name,
                   question_flag=question_flag)

    return render(request, template_name=template_name, context=context)


def pages(request, fname):

    # Add markdown file extension to markdown
    infile = f"{fname}.md"
    # Look for this file in static root.
    doc = os.path.join(settings.STATIC_ROOT, "forum", infile)

    if not os.path.exists(doc):
        messages.error(request, "File does not exist.")
        return redirect("post_list")

    admins = User.objects.filter(profile__role=Profile.MANAGER)
    mods = User.objects.filter(profile__role=Profile.MODERATOR).exclude(id__in=admins)
    admins = admins.prefetch_related("profile").order_by("-profile__score")
    mods = mods.prefetch_related("profile").order_by("-profile__score")
    context = dict(file_path=doc, tab=fname, admins=admins, mods=mods)

    return render(request, 'pages.html', context=context)


@is_moderator
def mark_spam(request, uid):
    """
    Mark post as spam.
    """

    # Trigger post
    post = Post.objects.filter(uid=uid).first()

    # A restore parameter sent toggles spam off.
    state = False if request.GET.get("restore") else True

    # Apply the toggle.
    if post:
        auth.toggle_spam(request, post)
    else:
        messages.error(request, "Post does not seem to exist")

    # Was spam actually
    if post.is_spam:
        return redirect('/?type=spam')
    else:
        return redirect('/')

@is_moderator
def release_quar(request, uid):
    """
    Release quarantined post to the public.
    """
    post = Post.objects.filter(uid=uid).first()
    if not post:
        messages.error(request, "Post does not exist.")
        return redirect('/')

    # Bump the score by one is the user does not get quarantined again.
    # Tells the system user has gained antibodies!
    if post.author.profile.low_rep:
        post.author.profile.bump_over_threshold()

    Post.objects.filter(uid=uid).update(spam=Post.NOT_SPAM)

    return redirect('/')

@ensure_csrf_cookie
def post_list(request, topic=None, cache_key='', extra_context=dict(), template_name="post_list.html"):
    """
    Post listing. Filters, orders and paginates posts based on GET parameters.
    """
    # The user performing the request.
    user = request.user

    # Parse the GET parameters for filtering information
    page = request.GET.get('page', 1)
    order = request.GET.get("order", "")
    topic = topic or request.GET.get("type", "")
    limit = request.GET.get("limit", "")

    # Get posts available to users.
    posts = get_posts(user=user, topic=topic, order=order, limit=limit)

    # Create the paginator.
    msg = f"{page} {order} {topic} {limit}"
    paginator = CachedPaginator(cache_key=cache_key, object_list=posts, msg=msg, per_page=settings.POSTS_PER_PAGE)

    # Apply the post paging.
    posts = paginator.get_page(page)

    # Set the active tab.
    tab = topic or LATEST

    # Fill in context.
    context = dict(posts=posts, tab=tab, order=order, type=topic, limit=limit, avatar=True)
    context.update(extra_context)

    # Render the page.
    return render(request, template_name=template_name, context=context)


def latest(request):
    """
    Show latest post listing.
    """
    order = request.GET.get("order", "")
    tag = request.GET.get("tag", "")
    topic = request.GET.get("type", "")
    limit = request.GET.get("limit", "")

    # Only cache unfiltered posts.
    cache_off = (order or limit or tag or topic)
    cache_key = None if cache_off else LATEST_CACHE_KEY

    return post_list(request, cache_key=cache_key)


def authenticated(func):
    def _wrapper_(request, **kwargs):
        if request.user.is_anonymous:
            messages.error(request, "You need to be logged in to view this page.")
            return reverse('post_list')
        return func(request, **kwargs)
    return _wrapper_


@authenticated
def myvotes(request):
    """
    Show posts by user that received votes
    """
    page = request.GET.get('page', 1)

    votes = Vote.objects.filter(post__author=request.user).select_related('post', 'post__root',
                                                                            'author__profile').order_by("-date")
    # Create the paginator
    paginator = CachedPaginator(object_list=votes,
                                per_page=settings.POSTS_PER_PAGE)

    # Apply the votes paging.
    votes = paginator.get_page(page)

    # Clear the votes count.
    counts = request.session.get(COUNT_DATA_KEY, {})
    # Set votes count back to 0
    counts[VOTES_COUNT] = 0
    request.session.update(dict(counts=counts))

    context = dict(votes=votes, page=page, tab='myvotes')
    return render(request, template_name="user_votes.html", context=context)


def tags_list(request):
    """
    Show posts by user
    """
    page = request.GET.get('page', 1)
    query = request.GET.get('query', '')

    count = Count('post', filter=Q(post__is_toplevel=True))

    db_query = Q(name__icontains=query) if query else Q()
    cache_key = None if query else TAGS_CACHE_KEY

    tags = Tag.objects.annotate(nitems=count).filter(db_query)
    tags = tags.order_by('-nitems')

    # Create the paginator
    paginator = CachedPaginator(cache_key=cache_key,
                                object_list=tags,
                                per_page=settings.POSTS_PER_PAGE)

    # Apply the votes paging.
    tags = paginator.get_page(page)

    context = dict(tags=tags, tab='tags', query=query)

    return render(request, 'tags_list.html', context=context)


@authenticated
def myposts(request):
    """
    Show posts by user
    """
    return post_list(request, topic=MYPOSTS, template_name="user_myposts.html")


def post_topic(request, topic):
    """
    Show list of posts of a given type
    """
    return post_list(request, topic=topic)


@authenticated
def following(request):
    """
    Show posts followed by user.
    """
    return post_list(request, topic=FOLLOWING, template_name="user_following.html")


@authenticated
def bookmarks(request):
    """
    Show posts bookmarked by user.
    """

    return post_list(request, topic=BOOKMARKS, template_name="user_bookmarks.html")


@authenticated
def mytags(request):
    return post_list(request=request, topic=MYTAGS, template_name="user_mytags.html")


def community_list(request):

    users = User.objects.select_related("profile")

    page = request.GET.get("page", 1)
    ordering = request.GET.get("order", "")
    limit_to = request.GET.get("limit", '')
    query = request.GET.get('query', '')
    query = query.replace("'", "").replace('"', '').strip()

    days = LIMIT_MAP.get(limit_to, 0)

    if days:
        delta = util.now() - timedelta(days=days)
        users = users.filter(profile__last_login__gt=delta)

    if query and len(query) > 2:
        db_query = Q(profile__name__icontains=query) | \
                   Q(profile__uid__icontains=query) | \
                   Q(username__icontains=query)| \
                   Q(email__icontains=query)

        users = users.filter(db_query)

    # Remove the cache when filters are given.
    no_cache = days or (query and len(query) > 2) or ordering
    cache_key = None if no_cache else USERS_LIST_KEY

    order = ORDER_MAPPER.get(ordering, "-profile__last_login")
    users = users.filter(profile__state__in=[Profile.NEW, Profile.TRUSTED])
    users = users.order_by(order)

    # Create the paginator
    paginator = CachedPaginator(cache_key=cache_key, object_list=users,
                                per_page=settings.POSTS_PER_PAGE)
    users = paginator.get_page(page)
    context = dict(tab="community", users=users, query=query, order=ordering, limit=limit_to)

    return render(request, "community_list.html", context=context)


def badge_list(request):
    badges = Badge.objects.annotate(count=Count("award")).order_by('-count')
    context = dict(badges=badges)
    return render(request, "badge_list.html", context=context)


def badge_view(request, uid):
    badge = Badge.objects.filter(uid=uid).annotate(count=Count("award")).first()
    target = request.GET.get('user')
    page = request.GET.get('page', 1)

    user = User.objects.filter(profile__uid=target).first()

    if not badge:
        messages.error(request, f"Badge with id={uid} does not exist.")
        return redirect(reverse("badge_list"))

    awards = badge.award_set.all().order_by("-date")
    if user:
        awards = awards.filter(user=user)

    awards = awards.prefetch_related("user", "user__profile", "post", "post__root")
    paginator = Paginator(object_list=awards, per_page=settings.POSTS_PER_PAGE)

    awards = paginator.get_page(page)
    context = dict(awards=awards, badge=badge)

    return render(request, "badge_view.html", context=context)


@ensure_csrf_cookie
def post_view(request, uid):
    "Return a detailed view for specific post"

    # Get the post.
    post = Post.objects.filter(uid=uid).select_related('root').first()

    if not post:
        messages.error(request, "Post does not exist.")
        return redirect("post_list")

    if not post.is_toplevel:
        return redirect(post.get_absolute_url())

    # Form used for answers
    form = forms.PostShortForm(user=request.user, post=post)

    if request.method == "POST":

        form = forms.PostShortForm(data=request.POST, user=request.user, post=post)
        if form.is_valid():
            author = request.user
            content = form.cleaned_data.get("content")
            answer = auth.create_post(title=post.title, parent=post, author=author,
                                      content=content, ptype=Post.ANSWER, root=post.root)
            return redirect(answer.get_absolute_url())
        messages.error(request, form.errors)

    # Build the comment tree .
    root, comment_tree, answers, thread = auth.post_tree(user=request.user, root=post.root)
    # user string

    # Bump post views.
    models.update_post_views(post=post, request=request, timeout=settings.POST_VIEW_TIMEOUT)

    context = dict(post=root, tree=comment_tree, form=form, answers=answers)

    return render(request, "post_view.html", context=context)


@login_required
def new_post(request):
    """
    Creates a new post
    """

    form = forms.PostLongForm(user=request.user)
    author = request.user
    tag_val = content = ''
    if request.method == "POST":

        form = forms.PostLongForm(data=request.POST, user=request.user)
        tag_val = form.data.get('tag_val')
        content = form.data.get('content', '')
        if form.is_valid():
            # Create a new post by user
            title = form.cleaned_data.get('title')
            content = form.cleaned_data.get("content")
            ptype = form.cleaned_data.get('post_type')
            tag_val = form.cleaned_data.get('tag_val')
            post = auth.create_post(title=title, content=content, ptype=ptype, tag_val=tag_val, author=author)

            tasks.created_post.spool(pid=post.id)

            return redirect(post.get_absolute_url())

    # Action url for the form is the current view
    action_url = reverse("post_create")
    context = dict(form=form, tab="new", tag_val=tag_val, action_url=action_url,
                   content=content)

    return render(request, "new_post.html", context=context)


@post_exists
@login_required
def post_moderate(request, uid):
    """Used to make display post moderate form given a post request."""

    user = request.user
    post = Post.objects.filter(uid=uid).first()

    if request.method == "POST":
        form = forms.PostModForm(post=post, data=request.POST, user=user, request=request)

        if form.is_valid():
            action = form.cleaned_data.get('action')
            comment = form.cleaned_data.get('comment')
            url = auth.moderate(request=request, post=post, action=action, comment=comment)
            return redirect(url)
        else:
            errors = ','.join([err for err in form.non_field_errors()])
            messages.error(request, errors)
            return redirect(reverse("post_view", kwargs=dict(uid=post.root.uid)))
    else:
        form = forms.PostModForm(post=post, user=user, request=request)

    context = dict(form=form, post=post)
    return render(request, "forms/form_moderate.html", context)

@login_required
def view_logs(request):
    LIMIT = 300

    if 0 and request.user.is_superuser:
        logs = Log.objects.all().order_by("-id")[:LIMIT]

    elif request.user.profile.is_moderator:
        logs = Log.objects.all().select_related("user", "user__profile").order_by("-id")[:LIMIT]

    else:
        logs = Log.objects.filter(pk=0)

    logs = logs.select_related("user", "post", "user__profile", "post__author")

    context = dict(logs=logs)

    return render(request, "view_logs.html", context=context)
