import base64

from django.core.management.base import BaseCommand


def b64url(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class Command(BaseCommand):
    help = "Gera chaves VAPID para notificacoes Web Push."

    def handle(self, *args, **options):
        try:
            from cryptography.hazmat.primitives.asymmetric import ec
        except Exception as exc:
            raise SystemExit(
                "Instale as dependencias do requirements.txt antes de gerar as chaves Web Push. "
                f"Erro: {exc}"
            )

        private_key = ec.generate_private_key(ec.SECP256R1())
        private_number = private_key.private_numbers().private_value
        public_numbers = private_key.public_key().public_numbers()

        private_bytes = private_number.to_bytes(32, "big")
        public_bytes = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")

        self.stdout.write("Configure estas variaveis no Render:")
        self.stdout.write(f"WEBPUSH_VAPID_PUBLIC_KEY={b64url(public_bytes)}")
        self.stdout.write(f"WEBPUSH_VAPID_PRIVATE_KEY={b64url(private_bytes)}")
        self.stdout.write("WEBPUSH_VAPID_EMAIL=mailto:seu-email@exemplo.com")
