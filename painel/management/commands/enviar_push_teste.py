from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from painel.models import PushSubscription
from painel.views import create_system_notification, webpush_is_enabled


class Command(BaseCommand):
    help = "Envia uma notificacao Web Push de teste para um usuario."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="joao.martins", help="Login do usuario que recebera o teste.")
        parser.add_argument("--cd", default="806", help="CD exibido na notificacao.")

    def handle(self, *args, **options):
        username = options["username"]
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist as exc:
            raise CommandError(f"Usuario nao encontrado: {username}") from exc

        active_subscriptions = PushSubscription.objects.filter(usuario=user, ativo=True).count()
        notification = create_system_notification(
            user,
            "Teste de notificacao",
            "Se esta mensagem apareceu fora do sistema, o Push esta funcionando neste aparelho.",
            url=reverse("dashboard"),
            categoria="teste_push",
            cd_unidade=options["cd"],
        )

        if notification.enviada_push_em:
            self.stdout.write(self.style.SUCCESS(f"Push enviado para {username}. Assinaturas ativas: {active_subscriptions}"))
            return

        if not webpush_is_enabled():
            self.stdout.write(self.style.WARNING("Notificacao interna criada, mas Web Push nao esta habilitado neste ambiente."))
        elif not active_subscriptions:
            self.stdout.write(self.style.WARNING(f"Notificacao interna criada, mas {username} nao tem assinatura Push ativa."))
        else:
            self.stdout.write(self.style.WARNING("Notificacao interna criada, mas nenhuma assinatura Push aceitou o envio."))
