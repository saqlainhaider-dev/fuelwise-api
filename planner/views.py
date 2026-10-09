from django.db import connection
from django.http import HttpResponse, JsonResponse
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from planner.interactive import render_interactive_map
from planner.maps import render_map
from planner.models import Trip
from planner.provider import PlanningError
from planner.service import plan


class LocationInput(serializers.Field):
    def to_internal_value(self, value):
        if isinstance(value, str) and 0 < len(value.strip()) <= 240:
            return value.strip()
        if isinstance(value, dict) and set(value) == {"lat", "lng"}:
            import math

            if all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in value.values()
            ):
                if -90 <= value["lat"] <= 90 and -180 <= value["lng"] <= 180:
                    return value
        raise serializers.ValidationError(
            'Use {"lat": number, "lng": number} or a nonempty address up to 240 characters.'
        )


class TripInput(serializers.Serializer):
    start = LocationInput()
    finish = LocationInput()


def index(request):
    return JsonResponse(
        {
            "service": "Fuelwise API",
            "endpoint": "POST /api/trips/plan",
            "example": {
                "start": {"lat": 35.4676, "lng": -97.5164},
                "finish": {"lat": 35.0844, "lng": -106.6504},
            },
            "routing_call_limit": 3,
            "map": "Static SVG from saved trip; no external rendering calls.",
        }
    )


def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})


@api_view(["POST"])
def plan_trip(request):
    serializer = TripInput(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        result = plan(**serializer.validated_data)
    except PlanningError as exc:
        return Response({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)
    trip = Trip.objects.create(request=serializer.validated_data, result=result)
    result["id"] = str(trip.id)
    result["map_url"] = f"/trips/{trip.id}/map"
    result["view_url"] = f"/trips/{trip.id}/view"
    return Response(result, status=201)


@api_view(["GET"])
def trip_detail(request, trip_id):
    try:
        trip = Trip.objects.get(pk=trip_id)
    except Trip.DoesNotExist:
        return Response({"error": {"code": "not_found", "message": "Trip not found."}}, status=404)
    return Response(
        {
            **trip.result,
            "id": str(trip.id),
            "map_url": f"/trips/{trip.id}/map",
            "view_url": f"/trips/{trip.id}/view",
        }
    )


def trip_map(request, trip_id):
    trip = Trip.objects.filter(pk=trip_id).first()
    if trip is None:
        return JsonResponse({"error": "not_found"}, status=404)
    response = HttpResponse(render_map(trip.result), content_type="image/svg+xml")
    response["Cache-Control"] = "private, max-age=86400"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


def trip_page(request, trip_id):
    trip = Trip.objects.filter(pk=trip_id).first()
    if trip is None:
        return JsonResponse({"error": "not_found"}, status=404)
    response = HttpResponse(render_interactive_map(trip.result), content_type="text/html")
    response["Cache-Control"] = "private, max-age=86400"
    # Cross-origin tile hosts identify apps by Referer; Django's default
    # same-origin policy strips it and OSM's volunteer servers answer 403.
    response["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://basemaps.cartocdn.com https://tile.openstreetmap.org; "
        "font-src 'self'"
    )
    return response
