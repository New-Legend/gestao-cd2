# Generated manually for the Central CD Krill project.

import django.db.models.deletion
import painel.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0009_checklist_frota"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='checklistfrota',
            name='arla_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. ARLA'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='arla_ok',
            field=models.BooleanField(default=True, verbose_name='ARLA OK'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='bateria_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. bateria'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='bateria_ok',
            field=models.BooleanField(default=True, verbose_name='Bateria OK'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='calibragem_pneus_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. calibragem'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='calibragem_pneus_ok',
            field=models.BooleanField(default=True, verbose_name='Calibragem dos pneus OK'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='cintos_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. cintos'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='cintos_ok',
            field=models.BooleanField(default=True, verbose_name='Cintos de seguranca OK'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='cnh_motorista_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. CNH'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='cnh_motorista_ok',
            field=models.BooleanField(default=True, verbose_name='CNH do motorista OK'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='direcao_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. direcao'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='direcao_ok',
            field=models.BooleanField(default=True, verbose_name='Direcao OK'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='documento_motorista_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. documento motorista'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='documento_motorista_ok',
            field=models.BooleanField(default=True, verbose_name='Documento do motorista OK'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='lataria_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. lataria'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='lataria_ok',
            field=models.BooleanField(default=True, verbose_name='Lataria OK'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='rastreador_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. rastreador'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='rastreador_ok',
            field=models.BooleanField(default=True, verbose_name='Rastreador OK'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='suspensao_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. suspensao'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='suspensao_ok',
            field=models.BooleanField(default=True, verbose_name='Suspensao OK'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='tacografo_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. tacografo'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='tacografo_ok',
            field=models.BooleanField(default=True, verbose_name='Tacografo OK'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='vedacao_bau_obs',
            field=models.CharField(blank=True, max_length=180, verbose_name='Obs. vedacao bau'),
        ),
        migrations.AddField(
            model_name='checklistfrota',
            name='vedacao_bau_ok',
            field=models.BooleanField(default=True, verbose_name='Vedacao do bau OK'),
        ),
        migrations.CreateModel(
            name='MelhoriaSistema',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('cd_unidade', models.CharField(choices=[('806', 'CD 806 - Japui'), ('801', 'CD 801')], default='806', max_length=3, verbose_name='CD')),
                ('data', models.DateField(default=painel.models.today, verbose_name='Data')),
                ('observacao', models.TextField(blank=True, verbose_name='Observação')),
                ('criado_em', models.DateTimeField(auto_now_add=True)),
                ('atualizado_em', models.DateTimeField(auto_now=True)),
                ('titulo', models.CharField(max_length=160, verbose_name='Titulo da melhoria')),
                ('area', models.CharField(choices=[('geral', 'Geral'), ('recebimento', 'Recebimento'), ('ressuprimento', 'Ressuprimento'), ('separacao', 'Separacao'), ('expedicao', 'Expedicao'), ('frota', 'Frota'), ('equipamentos', 'Equipamentos'), ('relatorios', 'Relatorios'), ('acesso', 'Acesso/usuarios')], default='geral', max_length=40, verbose_name='Area')),
                ('solicitante', models.CharField(blank=True, max_length=120, verbose_name='Quem sugeriu')),
                ('descricao', models.TextField(verbose_name='Descricao da melhoria')),
                ('impacto', models.TextField(blank=True, verbose_name='Impacto esperado')),
                ('prioridade', models.CharField(choices=[('baixa', 'Baixa'), ('media', 'Media'), ('alta', 'Alta'), ('urgente', 'Urgente')], default='media', max_length=20, verbose_name='Prioridade')),
                ('status', models.CharField(choices=[('sugestao', 'Sugestao'), ('avaliando', 'Avaliando'), ('aprovada', 'Aprovada'), ('em_andamento', 'Em andamento'), ('concluida', 'Concluida'), ('pausada', 'Pausada'), ('descartada', 'Descartada')], default='sugestao', max_length=30, verbose_name='Status')),
                ('responsavel', models.CharField(blank=True, max_length=120, verbose_name='Responsavel')),
                ('previsao', models.DateField(blank=True, null=True, verbose_name='Previsao')),
                ('decisao', models.TextField(blank=True, verbose_name='Decisao/retorno')),
                ('criado_por', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-data', '-id'],
                'abstract': False,
            },
        ),
    ]
