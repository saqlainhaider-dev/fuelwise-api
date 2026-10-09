from django.urls import path, re_path

from planner import views
from planner.interactive import leaflet_asset

urlpatterns = [
    path("", views.index),
    path("health/", views.health),
    path("api/trips/plan", views.plan_trip),
    path("api/trips/<uuid:trip_id>", views.trip_detail),
    path("trips/<uuid:trip_id>/map", views.trip_map, name="trip-map"),
    path("trips/<uuid:trip_id>/view", views.trip_page, name="trip-view"),
    re_path(r"^static/leaflet/(?P<name>[a-z0-9./-]+)$", leaflet_asset, name="leaflet-asset"),
]
