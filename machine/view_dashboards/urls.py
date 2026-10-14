from django.urls import path, include
from .views import (
    ActivityLogListView,
    DashboardPageView,
    DesktopConnectView,
    DesktopCreateView,
    DesktopDetailView,
    DesktopListView,
    DesktopPowerView,
    DesktopSyncView,
    DesktopUpdateView,
    LoginView,
    LogoutView,
    UserProfileCreateView,
    UserProfileListView,
    UserProfileUpdateView,
    VMProfileCreateView,
    VMProfileListView,
    VMProfileUpdateView,
)

app_name = 'view_dashboards'

urlpatterns = [
    path(r'', DashboardPageView.as_view(), name="dashboard"),
    path(r'desktops/', DesktopListView.as_view(), name="desktop_list"),
    path(r'desktops/create/', DesktopCreateView.as_view(), name="desktop_create"),
    path(r'desktops/sync/', DesktopSyncView.as_view(), name="desktop_sync"),
    path(r'desktops/<int:pk>/', DesktopDetailView.as_view(), name="desktop_detail"),
    path(r'desktops/<int:pk>/edit/', DesktopUpdateView.as_view(), name="desktop_update"),
    path(r'desktops/<int:pk>/connect/', DesktopConnectView.as_view(), name="desktop_connect"),
    path(r'desktops/<int:pk>/<str:action>/', DesktopPowerView.as_view(), name="desktop_power"),
    path(r'vm-profiles/', VMProfileListView.as_view(), name="vm_profile_list"),
    path(r'vm-profiles/create/', VMProfileCreateView.as_view(), name="vm_profile_create"),
    path(r'vm-profiles/<int:pk>/edit/', VMProfileUpdateView.as_view(), name="vm_profile_update"),
    path(r'users/', UserProfileListView.as_view(), name="user_profile_list"),
    path(r'users/create/', UserProfileCreateView.as_view(), name="user_profile_create"),
    path(r'users/<int:pk>/edit/', UserProfileUpdateView.as_view(), name="user_profile_update"),
    path(r'activity/', ActivityLogListView.as_view(), name="activity_list"),
    path(r'login/', LoginView.as_view(), name="login"),
    path(r'logout/', LogoutView.as_view(), name="logout"),
]