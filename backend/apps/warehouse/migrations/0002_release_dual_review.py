# 放行申请按风险等级双人复核
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('warehouse', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 历史在途申请无签署链条，无法在新流程继续，统一置为过期终态
        migrations.RunSQL(
            sql="UPDATE wh_stock_out SET status='expired' WHERE status='pending'",
            reverse_sql=migrations.RunSQL.noop,
        ),
        # 物资增加风险等级
        migrations.AddField(
            model_name='goods',
            name='risk_level',
            field=models.CharField(
                choices=[('low', '低风险'), ('high', '高风险')],
                default='low', max_length=10, verbose_name='风险等级',
            ),
        ),
        # 放行申请工作流字段
        migrations.AddField(
            model_name='stockout',
            name='risk_level',
            field=models.CharField(
                choices=[('low', '低风险'), ('high', '高风险')],
                default='low', max_length=10, verbose_name='风险等级',
            ),
        ),
        migrations.AddField(
            model_name='stockout',
            name='goods_name',
            field=models.CharField(
                blank=True, default='', max_length=200, verbose_name='货物名称快照',
            ),
        ),
        migrations.AddField(
            model_name='stockout',
            name='goods_code',
            field=models.CharField(
                blank=True, default='', max_length=50, verbose_name='货物编码快照',
            ),
        ),
        migrations.AddField(
            model_name='stockout',
            name='current_step',
            field=models.CharField(
                choices=[
                    ('apply', '申请'), ('review', '第一复核'), ('release', '最终放行'),
                ],
                default='apply', max_length=20, verbose_name='当前步骤',
            ),
        ),
        migrations.AddField(
            model_name='stockout',
            name='expires_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='有效期至'),
        ),
        migrations.AddField(
            model_name='stockout',
            name='rejected_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='拒绝时间'),
        ),
        migrations.AddField(
            model_name='stockout',
            name='reject_step',
            field=models.CharField(
                blank=True, default='', max_length=20, verbose_name='拒绝步骤',
            ),
        ),
        migrations.AddField(
            model_name='stockout',
            name='rejected_by',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='rejected_stock_outs',
                to=settings.AUTH_USER_MODEL, verbose_name='拒绝人',
            ),
        ),
        migrations.AddField(
            model_name='stockout',
            name='reject_reason',
            field=models.TextField(blank=True, default='', verbose_name='拒绝原因'),
        ),
        migrations.AddField(
            model_name='stockout',
            name='terminate_reason',
            field=models.CharField(
                blank=True, default='', max_length=200, verbose_name='终止原因',
            ),
        ),
        migrations.AddField(
            model_name='stockout',
            name='released_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='放行时间'),
        ),
        migrations.AlterField(
            model_name='stockout',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', '待审批'),
                    ('pending_review', '待第一复核'),
                    ('pending_release', '待最终放行'),
                    ('rejected', '已拒绝'),
                    ('expired', '已过期'),
                    ('terminated', '已终止'),
                    ('released', '已放行'),
                    ('approved', '已通过'),
                    ('completed', '已完成'),
                ],
                default='pending_review', max_length=20, verbose_name='状态',
            ),
        ),
        migrations.AlterField(
            model_name='stockout',
            name='goods',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='stock_outs',
                to='warehouse.goods', verbose_name='货物',
            ),
        ),
        migrations.AlterField(
            model_name='stockout',
            name='operator',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='stock_out_applications',
                to=settings.AUTH_USER_MODEL, verbose_name='申请人',
            ),
        ),
        migrations.AlterModelOptions(
            name='stockout',
            options={
                'verbose_name': '出库放行申请',
                'verbose_name_plural': '出库放行申请',
                'db_table': 'wh_stock_out',
                'ordering': ['-created_at'],
            },
        ),
        # 旧审批记录模型替换为分步签署记录
        migrations.DeleteModel(name='Approval'),
        migrations.CreateModel(
            name='ReleaseSignature',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False, verbose_name='ID',
                )),
                ('step', models.CharField(
                    choices=[
                        ('apply', '申请'), ('review', '第一复核'), ('release', '最终放行'),
                    ],
                    max_length=20, verbose_name='签署步骤',
                )),
                ('action', models.CharField(
                    choices=[('approve', '通过'), ('reject', '拒绝')],
                    max_length=10, verbose_name='签署动作',
                )),
                ('goods_snapshot', models.JSONField(
                    blank=True, default=dict, verbose_name='物资摘要',
                )),
                ('remark', models.TextField(blank=True, default='', verbose_name='签署意见')),
                ('signed_at', models.DateTimeField(auto_now_add=True, verbose_name='签署时间')),
                ('signer', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='release_signatures',
                    to=settings.AUTH_USER_MODEL, verbose_name='签署人',
                )),
                ('stock_out', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='signatures',
                    to='warehouse.stockout', verbose_name='放行申请',
                )),
            ],
            options={
                'verbose_name': '放行签署记录',
                'verbose_name_plural': '放行签署记录',
                'db_table': 'wh_release_signature',
                'ordering': ['signed_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='releasesignature',
            constraint=models.UniqueConstraint(
                fields=('stock_out', 'step'),
                name='uniq_signature_per_stockout_step',
            ),
        ),
    ]
