from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0056_expedicaovinculo_exige_plataforma_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="veiculofrota",
            name="plataforma_operacional",
            field=models.BooleanField(default=True, verbose_name="Plataforma operacional"),
        ),
    ]
