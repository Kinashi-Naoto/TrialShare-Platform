# Generated by Django 3.2 on 2021-05-06 23:07

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('forum', '0017_expanded_log'),
    ]

    operations = [
        migrations.AlterField(
            model_name='post',
            name='type',
            field=models.IntegerField(choices=[(0, 'Question'), (1, 'Answer'), (6, 'Comment'), (2, 'Job'), (3, 'Forum'), (8, 'Tutorial'), (7, 'Data'), (4, 'Page'), (10, 'Tool'), (11, 'News'), (5, 'Blog'), (9, 'Bulletin Board'), (12, 'Herald')], db_index=True),
        ),
        migrations.CreateModel(
            name='SharedLink',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('url', models.URLField(max_length=10000)),
                ('text', models.TextField(blank=True, default='', max_length=10000)),
                ('creation_date', models.DateTimeField()),
                ('lastedit_date', models.DateTimeField()),
                ('status', models.IntegerField(choices=[(0, 'Submitted'), (1, 'Declined'), (3, 'Published'), (2, 'Accepted')], db_index=True, default=0)),
                ('author', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
                ('editor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='herald_editor', to=settings.AUTH_USER_MODEL)),
                ('post', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='forum.post')),
            ],
        ),
    ]
