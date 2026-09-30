from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils.html import escape
from django.utils.http import url_has_allowed_host_and_scheme

from .utils import pilot_trial_state
from .utils import bool_config


class LegacyHostBlockMiddleware:
    """Bloqueia enderecos antigos depois que o link novo estiver ativo."""

    ALLOWED_PATH_PREFIXES = ("/health/", "/status-saude/", "/manter-online/")
    DEFAULT_BLOCKED_HOSTS = {"assistente-cd-krill-teste.onrender.com"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.get_host().split(":")[0].lower()
        blocked_hosts = set(self.DEFAULT_BLOCKED_HOSTS)
        blocked_hosts.update({
            item.strip().lower()
            for item in getattr(settings, "BLOCK_LEGACY_HOSTS", [])
            if item and item.strip()
        })
        if host in blocked_hosts and not request.path.startswith(self.ALLOWED_PATH_PREFIXES):
            return self.blocked_response()
        return self.get_response(request)

    @staticmethod
    def blocked_response():
        new_url = getattr(settings, "NEW_PUBLIC_URL", "").strip()
        show_url = getattr(settings, "SHOW_NEW_PUBLIC_URL", False)
        link_html = ""
        if show_url and new_url:
            safe_url = escape(new_url)
            link_html = f'<a class="button" href="{safe_url}">Abrir novo endereco</a>'
        html = f"""<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>Endereco desativado</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: Arial, Helvetica, sans-serif;
      background: #eef3f9;
      color: #071b33;
    }}
    main {{
      width: min(520px, calc(100% - 32px));
      border: 1px solid #c9d7e6;
      border-radius: 18px;
      background: #fff;
      box-shadow: 0 18px 45px rgba(7, 27, 51, .12);
      padding: 28px;
    }}
    h1 {{ margin: 0 0 10px; font-size: 28px; }}
    p {{ margin: 0 0 18px; line-height: 1.5; color: #4d5f74; }}
    .button {{
      display: inline-block;
      border-radius: 12px;
      background: #0f6fc5;
      color: #fff;
      padding: 12px 16px;
      text-decoration: none;
      font-weight: 700;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Endereco desativado</h1>
    <p>Este endereco de teste foi desativado. Solicite o novo acesso ao responsavel.</p>
    {link_html}
  </main>
</body>
</html>"""
        return HttpResponse(html, status=410)


class DemoModeMiddleware:
    """Simula operacoes sem permitir gravacoes reais no banco."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
    ALLOWED_PATH_PREFIXES = (
        "/login/",
        "/sair/",
        "/trocar-cd/",
        "/modo-demonstracao/",
        "/status-versao/",
        "/manifest.webmanifest",
        "/service-worker.js",
        "/apple-touch-icon",
        "/favicon.ico",
        "/offline/",
        "/health/",
        "/status-saude/",
        "/manter-online/",
        "/static/",
        "/media/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.demo_mode_active = request.COOKIES.get("modo_demo") == "1"
        if (
            request.demo_mode_active
            and request.method.upper() not in self.SAFE_METHODS
            and not self._is_allowed_path(request.path)
        ):
            if self._wants_json(request):
                return JsonResponse(
                    {
                        "ok": True,
                        "simulado": True,
                        "message": "Modo demonstração ativo. A ação foi simulada e nenhum dado real foi alterado.",
                    }
                )
            return redirect(self._feedback_url(request))
        return self.get_response(request)

    @classmethod
    def _is_allowed_path(cls, path):
        return any(path.startswith(prefix) for prefix in cls.ALLOWED_PATH_PREFIXES)

    @staticmethod
    def _wants_json(request):
        return (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in request.headers.get("Accept", "")
            or request.headers.get("Content-Type", "").startswith("application/json")
        )

    def _feedback_url(self, request):
        target = request.META.get("HTTP_REFERER") or request.get_full_path() or "/"
        if not url_has_allowed_host_and_scheme(
            target,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            target = request.path or "/"
        return self._with_query(target, {"demo_salvo": "1"})

    @staticmethod
    def _with_query(url, extra):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(extra)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class SystemLockMiddleware:
    """Suspende o uso operacional sem bloquear o acesso master."""

    ALLOWED_USERNAMES = {"ping.sistema"}
    ALLOWED_PATH_PREFIXES = (
        "/login/",
        "/sair/",
        "/status-versao/",
        "/manifest.webmanifest",
        "/service-worker.js",
        "/apple-touch-icon",
        "/favicon.ico",
        "/offline/",
        "/health/",
        "/status-saude/",
        "/manter-online/",
        "/static/",
        "/media/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._is_allowed_path(request.path):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            return self.get_response(request)
        if str(getattr(user, "username", "") or "").strip().lower() in self.ALLOWED_USERNAMES:
            return self.get_response(request)
        if self._is_master(user):
            return self.get_response(request)

        try:
            locked = bool_config("sistema_bloqueado", True)
        except Exception:
            locked = False

        if locked:
            return self.locked_response()
        return self.get_response(request)

    @classmethod
    def _is_allowed_path(cls, path):
        return any(path.startswith(prefix) for prefix in cls.ALLOWED_PATH_PREFIXES)

    @staticmethod
    def _is_master(user):
        if getattr(user, "is_superuser", False):
            return True
        try:
            profile = getattr(user, "perfil_krill", None)
        except Exception:
            profile = None
        return bool(profile and profile.cargo == "master")

    @staticmethod
    def locked_response():
        html = """<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>Uso suspenso</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: Arial, Helvetica, sans-serif; background: #eef3f9; color: #071b33; }
    main { width: min(560px, calc(100% - 32px)); border: 1px solid #c9d7e6; border-top: 6px solid #ffd21f; border-radius: 14px; background: #fff; box-shadow: 0 18px 45px rgba(7, 27, 51, .12); padding: 28px; }
    h1 { margin: 0 0 10px; font-size: 28px; }
    p { margin: 0 0 16px; line-height: 1.5; color: #4d5f74; }
    a { display: inline-block; border-radius: 10px; background: #0f6fc5; color: #fff; padding: 12px 16px; text-decoration: none; font-weight: 700; }
  </style>
</head>
<body>
  <main>
    <h1>Uso temporariamente suspenso</h1>
    <p>O acesso operacional foi pausado pelo responsavel pela ferramenta. Os dados permanecem preservados e o acesso sera liberado quando houver nova autorizacao.</p>
    <a href="/sair/">Voltar ao login</a>
  </main>
</body>
</html>"""
        return HttpResponse(html, status=423)


class PilotTrialMiddleware:
    """Controla o encerramento do piloto comercial."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
    ALLOWED_PATH_PREFIXES = (
        "/login/",
        "/sair/",
        "/status-versao/",
        "/manifest.webmanifest",
        "/service-worker.js",
        "/apple-touch-icon",
        "/favicon.ico",
        "/offline/",
        "/health/",
        "/status-saude/",
        "/manter-online/",
        "/static/",
        "/media/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            state = pilot_trial_state()
        except Exception:
            request.pilot_trial_state = {"enabled": False, "status": "error"}
            return self.get_response(request)
        request.pilot_trial_state = state
        user = getattr(request, "user", None)
        if not state.get("enabled") or self._is_allowed_path(request.path) or not getattr(user, "is_authenticated", False):
            return self.get_response(request)
        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return self.get_response(request)
        status = state.get("status")
        if status == "expired":
            return self.expired_response(state)
        if status == "read_only" and request.method.upper() not in self.SAFE_METHODS:
            return self.read_only_response(state)
        return self.get_response(request)

    @classmethod
    def _is_allowed_path(cls, path):
        return any(path.startswith(prefix) for prefix in cls.ALLOWED_PATH_PREFIXES)

    @staticmethod
    def expired_response(state):
        html = """<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>Piloto encerrado</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: Arial, Helvetica, sans-serif; background: #eef3f9; color: #071b33; }
    main { width: min(560px, calc(100% - 32px)); border: 1px solid #c9d7e6; border-top: 6px solid #ffd21f; border-radius: 14px; background: #fff; box-shadow: 0 18px 45px rgba(7, 27, 51, .12); padding: 28px; }
    h1 { margin: 0 0 10px; font-size: 28px; }
    p { margin: 0 0 16px; line-height: 1.5; color: #4d5f74; }
    a { display: inline-block; border-radius: 10px; background: #0f6fc5; color: #fff; padding: 12px 16px; text-decoration: none; font-weight: 700; }
  </style>
</head>
<body>
  <main>
    <h1>Periodo de teste encerrado</h1>
    <p>O piloto gratuito foi finalizado. A continuidade do uso depende da aprovacao comercial e liberacao do responsavel pelo sistema.</p>
    <a href="/sair/">Voltar ao login</a>
  </main>
</body>
</html>"""
        return HttpResponse(html, status=402)

    @staticmethod
    def read_only_response(state):
        html = """<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>Piloto em consulta</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: Arial, Helvetica, sans-serif; background: #eef3f9; color: #071b33; }
    main { width: min(560px, calc(100% - 32px)); border: 1px solid #c9d7e6; border-top: 6px solid #ffd21f; border-radius: 14px; background: #fff; box-shadow: 0 18px 45px rgba(7, 27, 51, .12); padding: 28px; }
    h1 { margin: 0 0 10px; font-size: 28px; }
    p { margin: 0 0 16px; line-height: 1.5; color: #4d5f74; }
    a { display: inline-block; border-radius: 10px; background: #0f6fc5; color: #fff; padding: 12px 16px; text-decoration: none; font-weight: 700; }
  </style>
</head>
<body>
  <main>
    <h1>Periodo de consulta</h1>
    <p>O piloto gratuito terminou. Novos lancamentos ficam bloqueados; os dados permanecem disponiveis temporariamente para conferencia e exportacao.</p>
    <a href="/">Voltar para consulta</a>
  </main>
</body>
</html>"""
        return HttpResponse(html, status=403)
