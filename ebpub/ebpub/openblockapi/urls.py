from django.conf.urls.defaults import *
from ebpub.openblockapi import views

urlpatterns = patterns(
    '',
    url(r'^$', views.check_api_available, name="check_api_available"),
    url(r'^items/types.json$', views.list_types_json, name="list_types_json"),
    url(r'^locations.json$', views.locations_json, name="locations_json"),
    url(r'^locations/(?P<slug>.*).json$', views.location_detail_json, name="location_detail_json"),
    url(r'^items.json$', views.items_json, name="items_json"),
    url(r'^items.atom$', views.items_json, name="items_atom"),
)
