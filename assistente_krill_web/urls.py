"""
URL configuration for assistente_krill_web project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path

from painel import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('home/', views.dashboard, name='home'),
    path('gestao/', views.painel_gestao, name='painel_gestao'),
    path('central-servidor/', views.central_servidor, name='central_servidor'),
    path('login/', views.login_view, name='login'),
    path('esqueci-senha/', views.solicitar_senha, name='solicitar_senha'),
    path('solicitar-acesso/', views.solicitar_acesso, name='solicitar_acesso'),
    path('status-versao/', views.app_status, name='app_status'),
    path('manifest.webmanifest', views.pwa_manifest, name='pwa_manifest'),
    path('service-worker.js', views.service_worker, name='service_worker'),
    path('apple-touch-icon.png', views.pwa_icon, {"filename": "apple-touch-icon.png"}, name='apple_touch_icon'),
    path('apple-touch-icon-precomposed.png', views.pwa_icon, {"filename": "apple-touch-icon-precomposed.png"}, name='apple_touch_icon_precomposed'),
    path('favicon.ico', views.pwa_icon, {"filename": "favicon.ico"}, name='favicon'),
    path('push/public-key/', views.push_public_key, name='push_public_key'),
    path('push/status/', views.push_status, name='push_status'),
    path('push/subscribe/', views.push_subscribe, name='push_subscribe'),
    path('push/unsubscribe/', views.push_unsubscribe, name='push_unsubscribe'),
    path('push/teste/', views.push_test, name='push_test'),
    path('notificacoes/lida/', views.notification_read, name='notification_read'),
    path('offline/', views.offline, name='offline'),
    path('health/', views.health_status, name='health'),
    path('status-saude/', views.health_status, name='health_status'),
    path('sentinela/', views.sentinela_servidor, name='sentinela_servidor'),
    path('manter-online/', views.keepalive, name='keepalive'),
    path('frota/', views.frota_hub, name='frota_hub'),
    path('colaboradores/', views.colaboradores_hub, name='colaboradores_hub'),
    path('central/<slug:key>/', views.central_personalizada, name='central_personalizada'),
    path('sair/', views.logout_view, name='logout'),
    path('modo-demonstracao/', views.modo_demonstracao, name='modo_demonstracao'),
    path('trocar-cd/', views.set_cd, name='set_cd'),
    path('modulo/<str:key>/data=<path:legacy_query>', views.module_legacy_query_redirect, name='module_legacy_query_redirect'),
    path('modulo/<str:key>/', views.module_list, name='module_list'),
    path('modulo/<str:key>/<int:pk>/editar/', views.module_edit, name='module_edit'),
    path('modulo/<str:key>/<int:pk>/excluir/', views.module_delete, name='module_delete'),
    path('usuarios/', views.usuarios, name='usuarios'),
    path('minha-tela/', views.preferencias, name='preferencias'),
    path('recursos-sistema/', views.recursos_sistema, name='recursos_sistema'),
    path('apresentacao-sistema/', views.apresentacao_sistema, name='apresentacao_sistema'),
    path('treinamento-sistema/', views.treinamento_sistema, name='treinamento_sistema'),
    path('notificacoes-push/', views.notificacoes_push, name='notificacoes_push'),
    path('personalizar-interface/', views.personalizar_interface, name='personalizar_interface'),
    path('perfil/', views.perfil_usuario, name='perfil_usuario'),
    path('minhas-solicitacoes/', views.minhas_solicitacoes, name='minhas_solicitacoes'),
    path('painel-master/', views.auditoria, name='auditoria'),
    path('diagnostico/', views.diagnostico, name='diagnostico'),
    path('relatorios/', views.relatorios, name='relatorios'),
    path('importar/', views.importar, name='importar'),
    path('corrigir-cd/', views.corrigir_cd, name='corrigir_cd'),
    path('admin/', admin.site.urls),
]
