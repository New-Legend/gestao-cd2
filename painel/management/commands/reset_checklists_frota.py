from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from painel.models import ChecklistFrota


class Command(BaseCommand):
    help = "Encerra ou apaga check-lists da Frota para recomecar a operacao."

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Confirma a execucao. Sem esta opcao o comando apenas mostra o que faria.",
        )
        parser.add_argument(
            "--delete-history",
            action="store_true",
            help="Apaga todos os check-lists, incluindo historico.",
        )
        parser.add_argument(
            "--cd",
            choices=["801", "805", "806"],
            help="Limita a limpeza a um CD especifico.",
        )
        parser.add_argument(
            "--motivo",
            default="Reset operacional solicitado para recomecar os testes.",
            help="Motivo registrado quando o comando apenas encerra check-lists em aberto.",
        )

    def handle(self, *args, **options):
        execute = options["execute"]
        delete_history = options["delete_history"]
        cd = options.get("cd")
        motivo = options["motivo"]

        queryset = ChecklistFrota.objects.all()
        if cd:
            queryset = queryset.filter(cd_unidade=cd)

        total = queryset.count()
        open_queryset = queryset.filter(
            tipo_checklist="saida",
            acompanhar_retorno=True,
            retorno_registrado__isnull=True,
            retorno_dispensado=False,
        )
        open_total = open_queryset.count()

        if not execute:
            self.stdout.write(self.style.WARNING("Simulacao: nada foi alterado."))
            if delete_history:
                self.stdout.write(f"Apagaria {total} check-list(s){f' do CD {cd}' if cd else ''}.")
            else:
                self.stdout.write(f"Encerraria {open_total} check-list(s) em aberto{f' do CD {cd}' if cd else ''}.")
            self.stdout.write("Para confirmar, rode novamente com --execute.")
            return

        with transaction.atomic():
            if delete_history:
                deleted, detail = queryset.delete()
                self.stdout.write(self.style.SUCCESS(f"Historico de check-list zerado: {deleted} registro(s) removido(s)."))
                for model_name, count in sorted(detail.items()):
                    self.stdout.write(f"- {model_name}: {count}")
                return

            updated = open_queryset.update(
                retorno_dispensado=True,
                retorno_dispensado_motivo=motivo,
                retorno_dispensado_em=timezone.now(),
            )
            self.stdout.write(self.style.SUCCESS(f"Check-list(s) em aberto encerrado(s): {updated}."))
