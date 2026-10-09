import uuid

from django.contrib.gis.db import models


class Station(models.Model):
    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=240)
    address = models.CharField(max_length=300)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    price = models.DecimalField(max_digits=12, decimal_places=8)
    source_rows = models.JSONField(default=list)
    issues = models.JSONField(default=list)
    source_hash = models.CharField(max_length=64)
    location = models.PointField(geography=True, null=True)
    access_location = models.PointField(geography=True, null=True)
    status = models.CharField(max_length=16, default="pending", db_index=True)
    here_id = models.CharField(max_length=240, blank=True)
    match_evidence = models.JSONField(default=dict)
    geocoded_at = models.DateTimeField(null=True)


class ProviderUsage(models.Model):
    service = models.CharField(max_length=16, unique=True)
    requests = models.PositiveIntegerField(default=0)


class ProviderCache(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    payload = models.JSONField(default=dict)
    expires_at = models.DateTimeField(db_index=True)


class Trip(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    request = models.JSONField()
    result = models.JSONField()
