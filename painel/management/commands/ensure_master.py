import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from painel.models import PerfilAcesso
from painel.registry import ALL_PERMISSION_KEYS


class Command(BaseCommand):
    help = "Cria ou atualiza o usuario master joao.martins."

    def handle(self, *args, **options):
        user = User.objects.filter(username="joao.martins").first()
        password = (
            os.environ.get("MODELO_TESTE_MASTER_PASSWORD")
            or os.environ.get("KRILL_MASTER_PASSWORD")
            or ""
        ).strip()
        if len(password) < 6 and not (user and user.has_usable_password()):
            raise CommandError("Defina MODELO_TESTE_MASTER_PASSWORD com pelo menos 6 caracteres.")

        if user:
            created = False
        else:
            user = User.objects.create(username="joao.martins")
            created = True
        if password and (created or not user.has_usable_password()):
            user.set_password(password)
        if not user.first_name:
            user.first_name = "Joao"
        if not user.last_name:
            user.last_name = "Martins"
        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        user.save()

        profile, _ = PerfilAcesso.objects.get_or_create(user=user)
        profile.cargo = "master"
        profile.cd_padrao = "806"
        profile.permissoes = ALL_PERMISSION_KEYS
        profile.abas_ocultas = []
        profile.save()

        if created:
            self.stdout.write(self.style.SUCCESS("Usuario master joao.martins criado."))
        else:
            self.stdout.write(self.style.SUCCESS("Usuario master joao.martins confirmado sem redefinir senha."))
