from django.urls import path
from view_api import views

app_name = 'view_api'

urlpatterns = [
    path(r'desktops/', views.desktop_list, name='desktop-list'),
    path(r'desktops/sync/', views.desktop_sync, name='desktop-sync'),
    path(r'desktops/<int:pk>/start/', views.desktop_start, name='desktop-start'),
    path(r'desktops/<int:pk>/stop/', views.desktop_stop, name='desktop-stop'),
    path(r'desktops/<int:pk>/restart/', views.desktop_restart, name='desktop-restart'),
]
