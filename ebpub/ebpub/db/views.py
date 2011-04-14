#   Copyright 2007,2008,2009,2011 Everyblock LLC, OpenPlans, and contributors
#
#   This file is part of ebpub
#
#   ebpub is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   ebpub is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with ebpub.  If not, see <http://www.gnu.org/licenses/>.
#

from django import template
from django.conf import settings
from django.contrib.gis.shortcuts import render_to_kml
from django.core import urlresolvers
from django.core.cache import cache
from django.db.models import Q
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseRedirect, HttpResponsePermanentRedirect
from django.shortcuts import render_to_response, get_object_or_404
from django.template.loader import select_template
from django.utils import simplejson
from django.utils.cache import patch_response_headers
from django.utils.datastructures import SortedDict
from django.views.decorators.cache import cache_page
from ebpub.constants import HIDE_ADS_COOKIE_NAME, HIDE_SCHEMA_INTRO_COOKIE_NAME
from ebpub.db import breadcrumbs
from ebpub.db import constants
from ebpub.db.models import AggregateDay, AggregateLocation, AggregateFieldLookup
from ebpub.db.models import NewsItem, Schema, SchemaField, Lookup, LocationType, Location, SearchSpecialCase
from ebpub.db.schemafilters import FilterError
from ebpub.db.schemafilters import SchemaFilterChain
from ebpub.db.utils import populate_attributes_if_needed, populate_schema, today
from ebpub.db.utils import make_search_buffer
from ebpub.db.utils import block_radius_value
from ebpub.db.utils import url_to_place
from ebpub.geocoder import SmartGeocoder, AmbiguousResult, DoesNotExist, GeocodingException, InvalidBlockButValidStreet
from ebpub.db.utils import get_place_info_for_request
from ebpub import geocoder
from ebpub.geocoder.parser.parsing import normalize, ParsingError
from ebpub.metros.allmetros import get_metro
from ebpub.preferences.models import HiddenSchema
from ebpub.streets.models import Street, City, Block, Intersection
from ebpub.streets.utils import full_geocode
from ebpub.utils.clustering.json import ClusterJSON
from ebpub.utils.clustering.shortcuts import cluster_newsitems
from ebpub.utils.dates import daterange, parse_date
from ebpub.utils.view_utils import eb_render
from ebpub.utils.view_utils import parse_pid

import datetime
import hashlib
import logging
import posixpath
import re
import urllib

logger = logging.getLogger('ebpub.db.views')

################################
# HELPER FUNCTIONS (NOT VIEWS) #
################################

def radius_urlfragment(radius):
    # XXX this should be done consistently with other filter url generation. #69
    radius = unicode(radius)
    return u'%s-block%s' % (radius, radius != '1' and 's' or '')

def radius_url(url, radius):
    """add a block radius to the url.
    url is assumed to already end in a path segment that specifies a block.
    """
    # XXX this should be done consistently with other filter url generation. #69
    radius = unicode(radius)
    return u'%s,%s/' % (url.rstrip('/'), radius_urlfragment(radius))

def has_staff_cookie(request):
    return request.COOKIES.get(settings.STAFF_COOKIE_NAME) == settings.STAFF_COOKIE_VALUE

def get_schema_manager(request):
    if has_staff_cookie(request):
        return Schema.objects
    else:
        return Schema.public_objects


def get_date_chart_agg_model(schemas, start_date, end_date, agg_model, kwargs=None):
    kwargs = kwargs or {}
    counts = {}
    for agg in agg_model.objects.filter(schema__id__in=[s.id for s in schemas], date_part__range=(start_date, end_date), **kwargs):
        counts.setdefault(agg.schema_id, {})[agg.date_part] = agg.total
    return get_date_chart(schemas, start_date, end_date, counts)

def get_date_chart(schemas, start_date, end_date, counts):
    """
    Returns a list that's used to display a date chart for the given
    schemas. Note that start_date and end_date should be datetime.date objects,
    NOT datetime.datetime objects.

    counts should be a nested dictionary: {schema_id: {date: count}}

    The list will be in order given by the `schemas` parameter.
    """
    result = []
    for schema in schemas:
        if schema.id not in counts:
            result.append({
                'schema': schema,
                'dates': [{'date': d, 'count': 0} for d in daterange(start_date, end_date)],
                'max_count': 0,
                'total_count': 0,
                'latest_date': None,
            })
        else:
            dates = [{'date': d, 'count': counts[schema.id].get(d, 0)} for d in daterange(start_date, end_date)]
            nonzero_dates = [d['date'] for d in dates if d['count']]
            if nonzero_dates:
                latest_date = max(nonzero_dates)
            else:
                latest_date = None
            result.append({
                'schema': schema,
                'dates': dates,
                'max_count': max(d['count'] for d in dates),
                'total_count': sum(d['count'] for d in dates),
                'latest_date': latest_date,
            })
    return result


def has_clusters(cluster_dict):
    """
    Determines whether the cluster_dict has any actual clusters.

    Catches the case where a queryset has no items that have been geocoded.

    Cluster dicts have keys which are scales, and values which are lists of
    Bunch objects, so this function simply tests to see if any of the lists are
    not empty.
    """
    return any(cluster_dict.values())

def block_bbox(block, radius):
    """
    Assumes `block' has `wkt' attribute
    """
    try:
        from osgeo import ogr
    except ImportError:
        import ogr
    env = ogr.CreateGeometryFromWkt(block.wkt).Buffer(radius).GetEnvelope()
    return (env[0], env[2], env[1], env[3])


##############
# AJAX VIEWS #
##############

def validate_address(request):
    # Validates that request.GET['address'] can be parsed with the address parser.
    if not request.GET.get('address', '').strip():
        raise Http404
    geocoder = SmartGeocoder()
    try:
        result = {'address': geocoder.geocode(request.GET['address'])['address']}
    except (DoesNotExist, ParsingError, InvalidBlockButValidStreet):
        result = {}
    except AmbiguousResult, e:
        if get_metro()['multiple_cities']:
            result = {'addresses': [add['address'] + ', ' + add['city'] for add in e.choices]}
        else:
            result = {'addresses': [add['address'] for add in e.choices]}
    return HttpResponse(simplejson.dumps(result), mimetype="application/javascript")

def ajax_wkt(request):
    # JSON -- returns a list of WKT strings for request.GET['q'].
    # If it can't be geocoded, the list is empty.
    # If it's ambiguous, the list has multiple elements.
    q = request.GET.get('q', '').strip()
    if not q:
        wkt_list = []
    else:
        try:
            result = full_geocode(q)
        except DoesNotExist:
            wkt_list = []
        except Exception:
            wkt_list = []
        else:
            if result['type'] == 'block':
                wkt_list = []
            elif result['type'] in ('location', 'place'):
                if result['ambiguous']:
                    wkt_list = [r.wkt for r in result['result']]
                else:
                    wkt_list = [result['result'].location.wkt]
            elif result['type'] == 'address':
                if result['ambiguous']:
                    wkt_list = [r['point'].wkt for r in result['result']]
                else:
                    wkt_list = [result['result']['point'].wkt]
            else:
                wkt_list = []
    return HttpResponse(simplejson.dumps(wkt_list), mimetype="application/javascript")

def ajax_map_popups(request):
    """
    JSON -- returns a list of lists for request.GET['q'] (a comma-separated
    string of NewsItem IDs).

    The structure of the inner lists is [newsitem_id, popup_html, schema_name]
    """
    try:
        newsitem_ids = map(int, request.GET['q'].split(','))
    except (KeyError, ValueError):
        raise Http404('Invalid query')
    if len(newsitem_ids) >= 400:
        raise Http404('Too many points') # Security measure.
    # Ordering by schema__id is an optimization for _map_popups().
    ni_list = list(NewsItem.objects.filter(id__in=newsitem_ids).select_related().order_by('schema__id'))
    result = map_popups(ni_list)
    return HttpResponse(simplejson.dumps(result), mimetype="application/javascript")


def map_popups(ni_list):
    """
    Given a list of newsitems, return a list of lists
    of the form [newsitem_id, popup_html, schema_name]
    """
    schemas = list(set([ni.schema for ni in ni_list]))
    populate_attributes_if_needed(ni_list, schemas)
    result = []
    current_schema = current_template = None
    for ni in ni_list:
        schema = ni.schema
        if current_schema != schema:
            template_list = ['db/snippets/newsitem_popup_list/%s.html' % schema.slug,
                             'db/snippets/newsitem_popup_list.html',
                             ]
            current_template = select_template(template_list)
            current_schema = schema
        html = current_template.render(template.Context({'newsitem': ni, 'schema': schema, }))
        result.append([ni.id, html, schema.name.title()])
    return result

def ajax_place_newsitems(request):
    """
    JSON -- expects request.GET['pid'] (a location ID) and
    request.GET['s'] (a schema ID).

    Returns a JSON mapping containing {'bunches': {scale: [list of clusters]},
                                       'ids': [list of newsitem ids]}
    where clusters are represented as [[list of newsitem IDs], [center x, y]]

    NB: the list of all newsitem IDs should be the union of all IDs in
    all the clusters.
    """
    try:
        s = Schema.public_objects.get(id=int(request.GET['s']))
    except (KeyError, ValueError, Schema.DoesNotExist):
        raise Http404('Invalid Schema')
    pid = request.GET.get('pid', '')
    place, block_radius, xy_radius = parse_pid(pid)
    if isinstance(place, Block):
        search_buffer = make_search_buffer(place.location.centroid, block_radius)
        newsitem_qs = NewsItem.objects.filter(location__bboverlaps=search_buffer)
    else:
        newsitem_qs = NewsItem.objects.filter(newsitemlocation__location__id=place.id)

    # Make the JSON output. Note that we have to call dumps() twice because the
    # bunches are a special case.
    ni_list = list(newsitem_qs.filter(schema__id=s.id).order_by('-item_date')[:50])
    bunches = simplejson.dumps(cluster_newsitems(ni_list, 26), cls=ClusterJSON)
    id_list = simplejson.dumps([ni.id for ni in ni_list])
    return HttpResponse('{"bunches": %s, "ids": %s}' % (bunches, id_list), mimetype="application/javascript")


def ajax_place_lookup_chart(request):
    """
    JSON -- expects request.GET['pid'] and request.GET['sf'] (a SchemaField ID).
    """
    try:
        sf = SchemaField.objects.select_related().get(id=int(request.GET['sf']), schema__is_public=True)
    except (KeyError, ValueError, SchemaField.DoesNotExist):
        raise Http404('Invalid SchemaField')
    place, block_radius, xy_radius = parse_pid(request.GET.get('pid', ''))
    qs = NewsItem.objects.filter(schema__id=sf.schema.id)
    filter_url = place.url()[1:]
    if isinstance(place, Block):
        search_buffer = make_search_buffer(place.location.centroid, block_radius)
        qs = qs.filter(location__bboverlaps=search_buffer)
        filter_url = radius_url(filter_url, block_radius)
    else:
        qs = qs.filter(newsitemlocation__location__id=place.id)
    total_count = qs.count()
    top_values = qs.top_lookups(sf, 10)
    return render_to_response('db/snippets/lookup_chart.html', {
        'lookup': {'sf': sf, 'top_values': top_values},
        'total_count': total_count,
        'schema': sf.schema,
        'filter_url': filter_url,
    })

def ajax_place_date_chart(request):
    """
    JSON -- expects request.GET['pid'] and request.GET['s'] (a Schema ID).
    """
    try:
        s = Schema.public_objects.get(id=int(request.GET['s']))
    except (KeyError, ValueError, Schema.DoesNotExist):
        raise Http404('Invalid Schema')
    place, block_radius, xy_radius = parse_pid(request.GET.get('pid', ''))
    qs = NewsItem.objects.filter(schema__id=s.id)
    filter_url = place.url()[1:]
    if isinstance(place, Block):
        search_buffer = make_search_buffer(place.location.centroid, block_radius)
        qs = qs.filter(location__bboverlaps=search_buffer)
        filter_url = radius_url(filter_url, block_radius)
    else:
        qs = qs.filter(newsitemlocation__location__id=place.id)
    # TODO: Ignore future dates
    end_date = qs.order_by('-item_date').values('item_date')[0]['item_date']
    start_date = end_date - datetime.timedelta(days=settings.DEFAULT_DAYS)
    counts = qs.filter(schema__id=s.id, item_date__gte=start_date, item_date__lte=end_date).date_counts()
    date_chart = get_date_chart([s], end_date - datetime.timedelta(days=settings.DEFAULT_DAYS), end_date, {s.id: counts})[0]
    return render_to_response('db/snippets/date_chart.html', {
        'schema': s,
        'date_chart': date_chart,
        'filter_url': filter_url,
        'filters': SchemaFilterChain(schema=s),
    })

def ajax_location_type_list(request):
    loc_types = LocationType.objects.order_by('plural_name').values('id', 'slug', 'plural_name')
    response = HttpResponse(mimetype='application/javascript')
    simplejson.dump(list(loc_types), response)
    return response

def ajax_location_list(request, loc_type_id):
    locations = Location.objects.filter(location_type__pk=loc_type_id, is_public=True).order_by('display_order').values('id', 'slug', 'name')
    if not locations:
        raise Http404()
    response = HttpResponse(mimetype='application/javascript')
    simplejson.dump(list(locations), response)
    return response

def ajax_location(request, loc_id):
    try:
        location = Location.objects.get(pk=int(loc_id))
    except (ValueError, Location.DoesNotExist):
        raise Http404()
    loc_obj = dict([(k, getattr(location, k)) for k in ('name', 'area', 'id', 'normalized_name', 'slug', 'population')])
    loc_obj['wkt'] = location.location.wkt
    loc_obj['centroid'] = location.centroid.wkt
    response = HttpResponse(mimetype='application/javascript')
    simplejson.dump(loc_obj, response)
    return response

#########
# VIEWS #
#########


def homepage(request):
    """Front page of the default OpenBlock theme.
    """

    end_date = today()
    start_date = end_date - datetime.timedelta(days=settings.DEFAULT_DAYS)
    end_date += datetime.timedelta(days=1)

    sparkline_schemas = list(Schema.public_objects.filter(allow_charting=True, is_special_report=False))

    # Order by slug to ensure case-insensitive ordering. (Kind of hackish.)
    lt_list = LocationType.objects.filter(is_significant=True).order_by('slug').extra(select={'count': 'select count(*) from db_location where is_public=True and location_type_id=db_locationtype.id'})
    street_count = Street.objects.count()
    more_schemas = Schema.public_objects.filter(allow_charting=False).order_by('name')

    # Get the public records.
    date_charts = get_date_chart_agg_model(sparkline_schemas, start_date, end_date, AggregateDay)
    empty_date_charts, non_empty_date_charts = [], []
    for chart in date_charts:
        if chart['total_count']:
            non_empty_date_charts.append(chart)
        else:
            empty_date_charts.append(chart)
    non_empty_date_charts.sort(lambda a, b: cmp(b['total_count'], a['total_count']))
    empty_date_charts.sort(lambda a, b: cmp(a['schema'].plural_name, b['schema'].plural_name))

    return eb_render(request, 'homepage.html', {
        'location_type_list': lt_list,
        'street_count': street_count,
        'more_schemas': more_schemas,
        'non_empty_date_charts': non_empty_date_charts,
        'empty_date_charts': empty_date_charts,
        'num_days': settings.DEFAULT_DAYS,
        'default_lon': settings.DEFAULT_MAP_CENTER_LON,
        'default_lat': settings.DEFAULT_MAP_CENTER_LAT,
        'default_zoom': settings.DEFAULT_MAP_ZOOM,
        'bodyclass': 'homepage',
        'breadcrumbs': breadcrumbs.home({}),
    })

def search(request, schema_slug=''):
    "Performs a location search and redirects to the address/xy page."
    # Check whether a schema was provided.
    if schema_slug:
        try:
            schema = get_schema_manager(request).get(slug=schema_slug)
        except Schema.DoesNotExist:
            raise Http404('Schema does not exist')
        url_prefix = schema.url()[:-1]
    else:
        schema = None
        url_prefix = ''

    # Get the query.
    q = request.GET.get('q', '').strip()
    if not q:
        return HttpResponseRedirect(url_prefix + '/') # TODO: Do something better than redirecting.

    # For /search/?type=alert, we redirect results to the alert page, not the
    # place page.
    if request.GET.get('type', '') == 'alert':
        url_method = 'alert_url'
    else:
        url_method = 'url'

    # Try to geocode it using full_geocode().
    try:
        result = full_geocode(q, search_places=False)
    except: # TODO: Naked except clause.
        pass
    else:
        if result['ambiguous']:
            if result['type'] == 'block':
                return eb_render(request, 'db/search_invalid_block.html', {
                    'query': q,
                    'choices': result['result'],
                    'street_name': result['street_name'],
                    'block_number': result['block_number']
                })
            else:
                return eb_render(request, 'db/did_you_mean.html', {'query': q, 'choices': result['result']})
        elif result['type'] == 'location':
            return HttpResponseRedirect(url_prefix + getattr(result['result'], url_method)())
        elif result['type'] == 'address':
            # Block
            if result['result']['block']:
                return HttpResponseRedirect(url_prefix + getattr(result['result']['block'], url_method)())
            # Intersection
            try:
                intersection = Intersection.objects.get(id=result['result']['intersection_id'])
            except Intersection.DoesNotExist:
                pass
            else:
                return HttpResponseRedirect(url_prefix + getattr(intersection, url_method)())

    # Failing the geocoding, look in the special-case table.
    try:
        special_case = SearchSpecialCase.objects.get(query=normalize(q))
    except SearchSpecialCase.DoesNotExist:
        pass
    else:
        if special_case.redirect_to:
            return HttpResponseRedirect(special_case.redirect_to)
        else:
            return eb_render(request, 'db/search_special_case.html', {'query': q, 'special_case': special_case})

    # Failing that, display a list of ZIP codes if this looks like a ZIP.
    if re.search(r'^\s*\d{5}(?:-\d{4})?\s*$', q):
        z_list = Location.objects.filter(location_type__slug='zipcodes', is_public=True).select_related().order_by('name')
        if z_list:
            return eb_render(request, 'db/search_error_zip_list.html', {'query': q, 'zipcode_list': z_list})

    # Failing all of that, display the search error page.
    lt_list = LocationType.objects.filter(is_significant=True).order_by('name')
    return eb_render(request, 'db/search_error.html', {'query': q, 'locationtype_list': lt_list})

def newsitem_detail(request, schema_slug, year, month, day, newsitem_id):
    try:
        date = datetime.date(int(year), int(month), int(day))
    except ValueError:
        raise Http404('Invalid day')
    ni = get_object_or_404(NewsItem.objects.select_related(), id=newsitem_id)

    if ni.schema.slug != schema_slug or ni.item_date != date:
        raise Http404
    if not ni.schema.is_public and not has_staff_cookie(request):
        raise Http404('Not public')

    if not ni.schema.has_newsitem_detail:
        return HttpResponsePermanentRedirect(ni.url)

    atts = ni.attributes_for_template()

    has_location = ni.location is not None

    if has_location:
        locations_within = Location.objects.select_related().filter(
            newsitemlocation__news_item__id=ni.id)
        center_x = ni.location.centroid.x
        center_y = ni.location.centroid.y
    else:
        locations_within = ()
        center_x = settings.DEFAULT_MAP_CENTER_LON
        center_y = settings.DEFAULT_MAP_CENTER_LAT

    hide_ads = (request.COOKIES.get(HIDE_ADS_COOKIE_NAME) == 't')

    templates_to_try = ('db/newsitem_detail/%s.html' % ni.schema.slug, 'db/newsitem_detail.html')

    # Try to find a usable URL to link to from the location name.
    # TODO: move this logic to NewsItem.location_url()
    location_url = ni.location_url()
    if not location_url:
        # There might be any number of intersecting locations_within,
        # and we don't have any criteria for deciding which if any
        # best corresponds to ni.location_name; but we can try
        # a few other fallbacks.
        if ni.block:
            location_url = ni.block.url()
        elif ni.location:
            # Try reverse-geocoding and see if we get a block.
            try:
                block, distance = geocoder.reverse.reverse_geocode(ni.location)
                # TODO: if this happens, we should really update
                # ni.block, but this seems like a terrible place to do
                # that.
                logger.warn(
                    "%r (id %d) can be reverse-geocoded to %r (id %d) but"
                    " self.block isn't set" % (ni, ni.id, block, block.id))
                location_url = block.url()
            except geocoder.reverse.ReverseGeocodeError:
                logger.error(
                    "%r (id %d) has neither a location_url, nor a block,"
                    " nor a reverse-geocodable location" % (ni, ni.id))
                pass

    context = {
        'newsitem': ni,
        'attribute_list': [att for att in atts if att.sf.display],
        'attribute_dict': dict((att.sf.name, att) for att in atts),
        'has_location': has_location,
        'locations_within': locations_within,
        'location_url': location_url,
        'hide_ads': hide_ads,
        'map_center_x': center_x,
        'map_center_y': center_y,
        'bodyclass': 'newsitem-detail',
        'bodyid': schema_slug,
    }
    context['breadcrumbs'] = breadcrumbs.newsitem_detail(context)
    return eb_render(request, templates_to_try, context)

def schema_list(request):
    schema_list = Schema.objects.select_related().filter(is_public=True, is_special_report=False).order_by('plural_name')
    schemafield_list = list(SchemaField.objects.filter(is_filter=True).order_by('display_order'))
    browsable_locationtype_list = LocationType.objects.filter(is_significant=True)
    # Populate s_list, which contains a schema and schemafield list for each schema.
    s_list = []
    for s in schema_list:
        s_list.append({
            'schema': s,
            'schemafield_list': [sf for sf in schemafield_list if sf.schema_id == s.id],
        })

    return eb_render(request, 'db/schema_list.html', {
        'schema_list': s_list,
        'browsable_locationtype_list': browsable_locationtype_list,
        'bodyclass': 'schema-list',
    })

def schema_detail(request, slug):
    s = get_object_or_404(get_schema_manager(request), slug=slug)
    if s.is_special_report:
        return schema_detail_special_report(request, s)

    location_type_list = LocationType.objects.filter(is_significant=True).order_by('slug')

    if s.allow_charting:
        # For the date range, the end_date is the last non-future date
        # with at least one NewsItem.
        try:
            end_date = NewsItem.objects.filter(schema__id=s.id, item_date__lte=today()).values_list('item_date', flat=True).order_by('-item_date')[0]
        except IndexError:
            latest_dates = date_chart = ()
            start_date = end_date = None
        else:
            start_date = end_date - datetime.timedelta(days=constants.NUM_DAYS_AGGREGATE)
            date_chart = get_date_chart_agg_model([s], start_date, end_date, AggregateDay)[0]
            latest_dates = [date['date'] for date in date_chart['dates'] if date['count']]

        # Populate schemafield_list and lookup_list.
        schemafield_list = list(s.schemafield_set.filter(is_filter=True).order_by('display_order'))
        # XXX this duplicates part of schema_filter()
        LOOKUP_MIN_DISPLAYED = 7
        LOOKUP_BUFFER = 4
        lookup_list = []
        for sf in schemafield_list:
            if not (sf.is_charted and sf.is_lookup):
                continue
            top_values = list(AggregateFieldLookup.objects.filter(schema_field__id=sf.id).select_related('lookup').order_by('-total')[:LOOKUP_MIN_DISPLAYED + LOOKUP_BUFFER])
            if len(top_values) == LOOKUP_MIN_DISPLAYED + LOOKUP_BUFFER:
                top_values = top_values[:LOOKUP_MIN_DISPLAYED]
                has_more = True
            else:
                has_more = False
            lookup_list.append({'sf': sf, 'top_values': top_values, 'has_more': has_more})

        location_chartfield_list = []

        unknowns = AggregateLocation.objects.filter(
                schema__id=s.id,
                location__slug='unknown').select_related('location')
        unknown_dict = dict([(u.location_type_id, u.total) for u in unknowns])

        # Populate location_chartfield_list.
        for lt in location_type_list:
            # Collect the locations in the location_type here so we don't have
            # to query them again when grouping them with the newsitem totals
            locations = dict([(loc.id, loc) for loc in lt.location_set.iterator()])

            ni_totals = AggregateLocation.objects.filter(
                schema__id=s.id,
                location_type__id=lt.id,
                location__is_public=True).select_related('location').order_by('-total')

            if ni_totals:
                location_chartfield_list.append({'location_type': lt, 'locations': ni_totals[:9], 'unknown': unknown_dict.get(lt.id, 0)})
        ni_list = ()
    else:

        latest_dates = schemafield_list = date_chart = lookup_list = location_chartfield_list = ()
        ni_list = list(NewsItem.objects.filter(schema__id=s.id).order_by('-item_date')[:30])
        populate_schema(ni_list, s)
        populate_attributes_if_needed(ni_list, [s])

    textsearch_sf_list = list(SchemaField.objects.filter(schema__id=s.id, is_searchable=True).order_by('display_order'))
    boolean_lookup_list = [sf for sf in SchemaField.objects.filter(schema__id=s.id, is_filter=True, is_lookup=False).order_by('display_order') if sf.is_type('bool')]

    templates_to_try = ('db/schema_detail/%s.html' % s.slug, 'db/schema_detail.html')

    # The HIDE_SCHEMA_INTRO_COOKIE_NAME cookie is a comma-separated list of
    # schema IDs for schemas whose intro text should *not* be displayed.
    hide_intro = str(s.id) in request.COOKIES.get(HIDE_SCHEMA_INTRO_COOKIE_NAME, '').split(',')

    # XXX do we really need this here? can the template tags
    # just create an empty one if it doesn't exist?
    filterchain = SchemaFilterChain(schema=s)

    context = {
        'schema': s,
        'schemafield_list': schemafield_list,
        'location_type_list': location_type_list,
        'date_chart': date_chart,
        'lookup_list': lookup_list,
        'location_chartfield_list': location_chartfield_list,
        'boolean_lookup_list': boolean_lookup_list,
        'search_list': textsearch_sf_list,
        'newsitem_list': ni_list,
        'latest_dates': latest_dates[-3:],
        'hide_intro': hide_intro,
        'hide_intro_cookie_name': HIDE_SCHEMA_INTRO_COOKIE_NAME,
        'start_date': s.min_date,
        'end_date': today(),
        'bodyclass': 'schema-detail',
        'bodyid': slug,
        'filters': filterchain,
    }
    context['breadcrumbs'] = breadcrumbs.schema_detail(context)
    return eb_render(request, templates_to_try, context)

def schema_detail_special_report(request, schema):
    ni_list = NewsItem.objects.filter(schema__id=schema.id)
    populate_schema(ni_list, schema)
    populate_attributes_if_needed(ni_list, [schema])
    bunches = cluster_newsitems(ni_list, 26)

    if schema.allow_charting:
        browsable_locationtype_list = LocationType.objects.filter(is_significant=True)
        schemafield_list = list(schema.schemafield_set.filter(is_filter=True).order_by('display_order'))
    else:
        browsable_locationtype_list = []
        schemafield_list = []

    templates_to_try = ('db/schema_detail/%s.html' % schema.slug, 'db/schema_detail_special_report.html')
    return eb_render(request, templates_to_try, {
        'schema': schema,
        'newsitem_list': ni_list,
        'nothing_geocoded': not has_clusters(bunches),
        'all_bunches': simplejson.dumps(bunches, cls=ClusterJSON),
        'browsable_locationtype_list': browsable_locationtype_list,
        'schemafield_list': schemafield_list,
        'bodyclass': 'schema-detail-special-report',
        'bodyid': schema.slug,
    })

def schema_about(request, slug):
    s = get_object_or_404(get_schema_manager(request), slug=slug)
    context = {'schema': s, 'bodyid': slug, 'bodyclass': 'schema-about',}
    context['breadcrumbs'] = breadcrumbs.schema_about(context)
    return eb_render(request, 'db/schema_about.html', context)

class BadAddressException(Exception):
    def __init__(self, address, block_radius, address_choices, message=None):
        self.address = address
        self.block_radius = block_radius
        self.address_choices = address_choices
        self.message = message

class BadDateException(Exception):
    pass

def _schema_filter_normalize_url(request):
    """Returns a new URL, or None if the original URL is OK.

    Raises BadAddressException if the request contains an address
    that we fail to geocode.
    """
    # Due to the way our custom filter UI works, address, date and
    # text searches sometimes come in a query string instead of in the
    # URL. Here, we validate those searches and return a url for
    # redirection so that the arguments are in the path.
    #
    # TODO: normalize the order of queries in the final URL, so that
    # /by-foo=a,b;by-bar=x/ and /by-bar=x;by-foo=a,b/ are
    # normalized to the same URL?
    # That'd help cacheability and we could put less expensive filters first.
    # See #113

    # Get existing filter args from the URL.
    view, args, kwargs = urlresolvers.resolve(request.path)
    schemaslug, filter_args = args

    filter_args = (filter_args or '').rstrip('/')
    new_filter_args = ''

    # Build new filter args.
    # TODO: factor out a function that does this, #69
    if request.GET.get('address', '').strip():
        xy_radius, block_radius, cookies_to_set = block_radius_value(request)
        address = request.GET['address'].strip()
        result = None
        try:
            result = SmartGeocoder().geocode(address)
        except AmbiguousResult, e:
            raise BadAddressException(address, block_radius, address_choices=e.choices)
        except (GeocodingException, ParsingError):
            raise BadAddressException(address, block_radius, address_choices=())

        assert result
        if result['block']:
            block = result['block']
        elif result['intersection']:
            try:
                block = result['intersection'].blockintersection_set.all()[0].block
            except IndexError:
                # XXX Not sure this was deliberate, but we used to
                # call intersection.url() here, which does that
                # same blockintersection_set query, so it would
                # raise an IndexError here if there was no
                # matching block.  Preserving that behavior.
                # XXX should this be BadAddressException?
                raise
        else:
            # XXX todo: should this be BadAddressException?
            raise NotImplementedError('Reached invalid geocoding type: %r' % result)
        # TODO: factor out URL param format. #69
        address_range = '%d-%d' % (block.from_num, block.to_num)
        if block.predir:
            address_range += block.predir
        if block.postdir:
            address_range += '-' + block.postdir
        address_range = address_range.lower()
        new_filter_args = '%sstreets=%s' % (new_filter_args, ','.join(
                [block.street_slug, address_range, block_radius]))
    # End of address handling.

    if request.GET.get('start_date', '').strip() and request.GET.get('end_date', '').strip():
        try:
            start_date = parse_date(request.GET['start_date'], '%m/%d/%Y')
            end_date = parse_date(request.GET['end_date'], '%m/%d/%Y')
        except ValueError, e:
            raise BadDateException(str(e))
        if start_date.year < 1900 or end_date.year < 1900:
            # This prevents strftime from throwing a ValueError.
            raise BadDateException('Dates before 1900 are not supported.')

        # TODO: factor out URL param format. #69
        new_filter_args = '%sby-date=%s,%s' % (new_filter_args, start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))
    if request.GET.get('textsearch', '').strip() and request.GET.get('q', '').strip():
        # TODO: factor out URL param format. #69
        new_filter_args = '%sby-%s=%s' % (new_filter_args, request.GET['textsearch'], request.GET['q'])
    if not new_filter_args:
        return None

    # TODO: factor out URL param format. #69
    if filter_args.startswith('filter'):
        # Normalize to NOT include 'filter/'. I guess.
        filter_args = filter_args[6:]
    filter_args = filter_args.lstrip('/')
    if filter_args:
        filter_args = '%s;%s' % (filter_args, new_filter_args)
    else:
        filter_args =  new_filter_args
    filter_args = urllib.quote(filter_args)
    return urlresolvers.reverse(view, args=[schemaslug, filter_args], kwargs=kwargs)

def filter_reverse(slug, args):
    """Generate a reverse schema_filter URL.
    """
    for i, a  in enumerate(args):
        if isinstance(a, tuple) or isinstance(a, list):
            # The first item is the arg name, the rest are arg values.
            if len(a) > 1:
                name = a[0]
                values = ','.join(a[1:])
                args[i] = '%s=%s' % (name, values)
            else:
                # This is allowed eg. for showing a list of available
                # Blocks, or Lookup values, etc.
                args[i] = a[0]
        else:
            assert isinstance(a, basestring)
    argstring = urllib.quote(';'.join(args)) #['%s=%s' % (k, v) for (k, v) in args])
    if not argstring:
        argstring = 'filter'
    url = urlresolvers.reverse('ebpub-schema-filter', args=[slug])
    url = '%s/%s/' % (url, argstring)
    # Normalize duplicate slashes, dots, and the like.
    url = posixpath.normpath(url) + '/'
    return url


def schema_filter(request, slug, args_from_url):
    """
    List NewsItems for one schema, filtered by various criteria in the
    URL (date, location, or values of SchemaFields).
    """
    try:
        new_url = _schema_filter_normalize_url(request)
    except BadAddressException, e:
        return eb_render(request, 'db/filter_bad_address.html', {
                'address_choices': e.address_choices,
                'address': e.address,
                'radius': e.block_radius,
                'radius_url': radius_urlfragment(e.block_radius),
                })
    except BadDateException, e:
        raise Http404('<h1>%s</h1>' % str(e))

    if new_url is not None:
        return HttpResponseRedirect(new_url)

    s = get_object_or_404(get_schema_manager(request), slug=slug, is_special_report=False)
    if not s.allow_charting:
        return HttpResponsePermanentRedirect(s.url())

    context = {
        'bodyclass': 'schema-filter',
        'bodyid': s.slug,
        'schema': s,
        }
    # Breadcrumbs. We can assign this early because it's a generator,
    # so it'll get the full context no matter what.
    context['breadcrumbs'] = breadcrumbs.schema_filter(context)

    filter_sf_list = list(SchemaField.objects.filter(schema__id=s.id, is_filter=True).order_by('display_order'))
    textsearch_sf_list = list(SchemaField.objects.filter(schema__id=s.id, is_searchable=True).order_by('display_order'))

    # Use SortedDict to preserve the display_order.
    filter_sf_dict = SortedDict([(sf.slug, sf) for sf in filter_sf_list] + [(sf.slug, sf) for sf in textsearch_sf_list])

    # Create the initial QuerySet of NewsItems.
    start_date = s.min_date
    end_date = today()
    qs = NewsItem.objects.filter(schema__id=s.id, item_date__lte=end_date).order_by('-item_date')
    context['newsitem_qs'] = qs

    # Break apart the URL to determine what filters to apply.
    from ebpub.db.schemafilters import SchemaFilterChain

    try:
        filterchain = SchemaFilterChain.from_request(request, context, args_from_url, filter_sf_dict)
        filterchain = filterchain.normalized_clone()  # Optimal filter order.
        filters_need_more = filterchain.validate()
    except FilterError, e:
        if getattr(e, 'url', None) is not None:
            return HttpResponseRedirect(e.url)
        raise Http404(str(e))

    context['filters'] = filterchain

    if filters_need_more:
        # Show a page to select the unspecified value.
        context.update(filters_need_more)

        return eb_render(request, 'db/filter_lookup_list.html', context)

    qs = filterchain.apply(qs)

    #########################################################################
    # END OF FILTER LOOP

    # Get the list of top values for each lookup that isn't being filtered-by.
    # LOOKUP_MIN_DISPLAYED sets the number of records to display for each lookup
    # type. Normally, the UI displays a "See all" link, but the link is removed
    # if there are fewer than (LOOKUP_MIN_DISPLAYED + LOOKUP_BUFFER) records.
    LOOKUP_MIN_DISPLAYED = 7
    LOOKUP_BUFFER = 4
    lookup_list, boolean_lookup_list, search_list = [], [], []
    for sf in filter_sf_dict.values():
        if sf.is_searchable:
            search_list.append(sf)
        elif sf.is_type('bool'):
            boolean_lookup_list.append(sf)
        elif sf.is_lookup:
            top_values = AggregateFieldLookup.objects.filter(schema_field__id=sf.id).select_related('lookup').order_by('-total')[:LOOKUP_MIN_DISPLAYED+LOOKUP_BUFFER]
            top_values = list(top_values)
            if len(top_values) == LOOKUP_MIN_DISPLAYED + LOOKUP_BUFFER:
                top_values = top_values[:LOOKUP_MIN_DISPLAYED]
                has_more = True
            else:
                has_more = False
            lookup_list.append({'sf': sf, 'top_values': top_values, 'has_more': has_more})

    # Get the list of LocationTypes if a location filter has *not* been applied.
    if 'location' in filterchain:
        location_type_list = []
    else:
        location_type_list = LocationType.objects.filter(is_significant=True).order_by('slug')

    # Do the pagination. We don't use Django's Paginator class because it uses
    # SELECT COUNT(*), which we want to avoid.
    try:
        page = int(request.GET.get('page', '1'))
    except ValueError:
        raise Http404('Invalid page')
    idx_start = (page - 1) * constants.FILTER_PER_PAGE
    idx_end = page * constants.FILTER_PER_PAGE

    # Get one extra, so we can tell whether there's a next page.
    ni_list = list(qs[idx_start:idx_end+1])
    if page > 1 and not ni_list:
        raise Http404('No objects on page %s' % page)
    if len(ni_list) > constants.FILTER_PER_PAGE:
        has_next = True
        ni_list = ni_list[:-1]
    else:
        has_next = False
        idx_end = idx_start + len(ni_list)
    has_previous = page > 1

    populate_schema(ni_list, s)
    populate_attributes_if_needed(ni_list, [s])
    bunches = cluster_newsitems(ni_list, 26)

    if not context.get('bbox'):
        # Whole city map.
        context.update({
                'default_lon': settings.DEFAULT_MAP_CENTER_LON,
                'default_lat': settings.DEFAULT_MAP_CENTER_LAT,
                'default_zoom': settings.DEFAULT_MAP_ZOOM,
                })

    context.update({
        'newsitem_list': ni_list,

        # Pagination stuff
        'has_next': has_next,
        'has_previous': has_previous,
        'page_number': page,
        'previous_page_number': page - 1,
        'next_page_number': page + 1,
        'page_start_index': idx_start + 1,
        'page_end_index': idx_end,

        'nothing_geocoded': not has_clusters(bunches),
        'all_bunches': simplejson.dumps(bunches, cls=ClusterJSON),
        'lookup_list': lookup_list,
        'boolean_lookup_list': boolean_lookup_list,
        'search_list': search_list,
        'location_type_list': location_type_list,
        'date_filter_applied': filterchain.has_key('date'),
        'location_filter_applied': filterchain.has_key('location'),
        'lookup_descriptions': filterchain.lookup_descriptions,
        'start_date': start_date,
        'end_date': end_date,
    })
    return eb_render(request, 'db/filter.html', context)


def location_type_detail(request, slug):
    lt = get_object_or_404(LocationType, slug=slug)
    order_by = get_metro()['multiple_cities'] and ('city', 'display_order') or ('display_order',)
    loc_list = Location.objects.filter(location_type__id=lt.id, is_public=True).order_by(*order_by)
    lt_list = [{'location_type': i, 'is_current': i == lt} for i in LocationType.objects.filter(is_significant=True).order_by('plural_name')]
    context = {
        'location_type': lt,
        'location_list': loc_list,
        'location_type_list': lt_list,
        'bodyclass': 'location-type-detail',
        'bodyid': slug,
        }
    context['breadcrumbs'] = breadcrumbs.location_type_detail(context)
    return eb_render(request, 'db/location_type_detail.html', context)


def city_list(request):
    c_list = [City.from_norm_name(c['city']) for c in Street.objects.distinct().values('city').order_by('city')]
    return eb_render(request, 'db/city_list.html',
                     {'city_list': c_list,
                      'bodyclass': 'city-list',
                      })

def street_list(request, city_slug):
    city = city_slug and City.from_slug(city_slug) or None
    kwargs = city_slug and {'city': city.norm_name} or {}
    streets = list(Street.objects.filter(**kwargs).order_by('street', 'suffix'))
    if not streets:
        raise Http404('This city has no streets')
    context = {
        'street_list': streets,
        'city': city,
        'bodyclass': 'street-list',
    }
    context['breadcrumbs'] = breadcrumbs.street_list(context)
    return eb_render(request, 'db/street_list.html', context)

def block_list(request, city_slug, street_slug):
    city = city_slug and City.from_slug(city_slug) or None
    kwargs = {'street_slug': street_slug}
    if city_slug:
        city_filter = Q(left_city=city.norm_name) | Q(right_city=city.norm_name)
    else:
        city_filter = Q()
    blocks = Block.objects.filter(city_filter, **kwargs).order_by('postdir', 'predir', 'from_num', 'to_num')
    if not blocks:
        raise Http404
    context = {
        'block_list': blocks,
        'first_block': blocks[0],
        'city': city,
        'bodyclass': 'block-list',
    }
    context['breadcrumbs'] = breadcrumbs.block_list(context)
    return eb_render(request, 'db/block_list.html', context)



def place_detail_timeline(request, *args, **kwargs):
    context = get_place_info_for_request(request, *args, **kwargs)
    schema_manager = get_schema_manager(request)
    context['breadcrumbs'] = breadcrumbs.place_detail_timeline(context)
    newsitem_qs = context['newsitem_qs']
    is_latest_page = True
    # Check the query string for the max date to use. Otherwise, fall
    # back to today.
    end_date = today()
    if 'start' in request.GET:
        try:
            end_date = parse_date(request.GET['start'], '%m/%d/%Y')
            is_latest_page = False
        except ValueError:
            raise Http404

    # As an optimization, limit the NewsItems to those published in the
    # last few days.
    start_date = end_date - datetime.timedelta(days=settings.DEFAULT_DAYS)
    ni_list = newsitem_qs.filter(pub_date__gt=start_date-datetime.timedelta(days=1), pub_date__lt=end_date+datetime.timedelta(days=1)).select_related()
    if not has_staff_cookie(request):
        ni_list = ni_list.filter(schema__is_public=True)
    ni_list = ni_list.extra(
        select={'pub_date_date': 'date(db_newsitem.pub_date)'},
        order_by=('-pub_date_date', '-schema__importance', 'schema')
    )[:constants.NUM_NEWS_ITEMS_PLACE_DETAIL]
    #ni_list = smart_bunches(list(ni_list), max_days=5, max_items_per_day=100)

    # We're done filtering, so go ahead and do the query, to
    # avoid running it multiple times,
    # per http://docs.djangoproject.com/en/dev/topics/db/optimization
    ni_list = list(ni_list)
    schemas_used = list(set([ni.schema for ni in ni_list]))
    s_list = schema_manager.filter(is_special_report=False, allow_charting=True).order_by('plural_name')
    populate_attributes_if_needed(ni_list, schemas_used)
    bunches = cluster_newsitems(ni_list, 26)
    if ni_list:
        next_day = ni_list[len(ni_list)-1:][0].pub_date - datetime.timedelta(days=1)
        #next_day = ni_list[-1].pub_date - datetime.timedelta(days=1)
    else:
        next_day = None

    hidden_schema_list = []
    if not request.user.is_anonymous():
        hidden_schema_list = [o.schema for o in HiddenSchema.objects.filter(user_id=request.user.id)]

    context.update({
        'newsitem_list': ni_list,
        'nothing_geocoded': not has_clusters(bunches),
        'all_bunches': simplejson.dumps(bunches, cls=ClusterJSON),
        'next_day': next_day,
        'is_latest_page': is_latest_page,
        'hidden_schema_list': hidden_schema_list,
        'bodyclass': 'place-detail-timeline',
        'bodyid': context.get('place_type') or '',
    })


    context['filtered_schema_list'] = s_list

    response = eb_render(request, 'db/place_detail.html', context)
    for k, v in context['cookies_to_set'].items():
        response.set_cookie(k, v)
    return response


def place_detail_overview(request, *args, **kwargs):
    context = get_place_info_for_request(request, *args, **kwargs)
    schema_manager = get_schema_manager(request)
    context['breadcrumbs'] = breadcrumbs.place_detail_overview(context)
    newsitem_qs = context['newsitem_qs']

    # Here, the goal is to get the latest nearby NewsItems for each
    # schema. A naive way to do this would be to run the query once for
    # each schema, but we improve on that by grabbing the latest 300
    # items of ANY schema and hoping that several of the schemas include
    # all of their recent items in that list. Then, for any remaining
    # schemas, we do individual queries as a last resort.
    # Note that we iterate over the 300 NewsItems as the outer loop rather
    # than iterating over the schemas as the outer loop, because there are
    # many more NewsItems than schemas.
    s_list = SortedDict([(s.id, [s, [], 0]) for s in schema_manager.filter(is_special_report=False).order_by('plural_name')])
    needed = set(s_list.keys())
    for ni in newsitem_qs.order_by('-item_date', '-id')[:300]: # Ordering by ID ensures consistency across page views.
        s_id = ni.schema_id
        if s_id in needed:
            s_list[s_id][1].append(ni)
            s_list[s_id][2] += 1
            if s_list[s_id][2] == s_list[s_id][0].number_in_overview:
                needed.remove(s_id)
    sf_dict = {}
    for sf in SchemaField.objects.filter(is_lookup=True, is_charted=True, schema__is_public=True, schema__is_special_report=False).values('id', 'schema_id', 'pretty_name').order_by('schema__id', 'display_order'):
        sf_dict.setdefault(sf['schema_id'], []).append(sf)
    schema_blocks, all_newsitems = [], []
    for s, newsitems, _ in s_list.values():
        if s.id in needed:
            newsitems = list(newsitem_qs.filter(schema__id=s.id).order_by('-item_date', '-id')[:s.number_in_overview])
        populate_schema(newsitems, s)
        schema_blocks.append({
            'schema': s,
            'latest_newsitems': newsitems,
            'has_newsitems': bool(newsitems),
            'lookup_charts': sf_dict.get(s.id),
        })
        all_newsitems.extend(newsitems)
    s_list = [s[0] for s in s_list.values()]
    populate_attributes_if_needed(all_newsitems, s_list)
    s_list = [s for s in s_list if s.allow_charting]

    context['schema_blocks'] = schema_blocks
    context['filtered_schema_list'] = s_list
    context['bodyclass'] = 'place-detail-overview'
    if context['is_block']:
        context['bodyid'] = '%s-%s-%s' % (context['place'].street_slug,
                                          context['place'].number(),
                                          context['place'].dir_url_bit())
    else:
        context['bodyid'] = context['location'].slug

    response = eb_render(request, 'db/place_overview.html', context)
    for k, v in context['cookies_to_set'].items():
        response.set_cookie(k, v)
    return response


def feed_signup(request, *args, **kwargs):
    context = get_place_info_for_request(request, *args, **kwargs)
    context['s_list'] = get_schema_manager(request).filter(is_special_report=False).order_by('plural_name')
    return eb_render(request, 'db/feed_signup.html', context)


def newsitems_geojson(request):
    """Get a list of newsitems, optionally filtered for one place ID
    and/or one schema slug.

    Response is a geojson string.
    """
    # Note: can't use @cache_page here because that ignores all requests
    # with query parameters (in FetchFromCacheMiddleware.process_request).
    # So, we'll use the low-level cache API.

    # Copy-pasted code from ajax_place_newsitems.  Refactoring target:
    # Seems like there are a number of similar code blocks in
    # ebpub.db.views?

    pid = request.GET.get('pid', '')
    schema = request.GET.get('schema', '')
    nid = request.GET.get('newsitem', '')

    cache_seconds = 60 * 5
    cache_key = 'newsitem_geojson_%s_%s' % (pid, schema)

    if nid:
        newsitem_qs = NewsItem.objects.filter(id=nid)
    else:
        newsitem_qs = NewsItem.objects.all()
        if schema:
            newsitem_qs = newsitem_qs.filter(schema__slug=schema)
        if pid:
            place, block_radius, xy_radius = parse_pid(pid)
            if isinstance(place, Block):
                search_buffer = make_search_buffer(place.location.centroid, block_radius)
                newsitem_qs = newsitem_qs.filter(location__bboverlaps=search_buffer)
            else:
                # This depends on the trigger in newsitemlocation.sql
                newsitem_qs = newsitem_qs.filter(
                    newsitemlocation__location__id=place.id)
        else:
            # Whole city!
            pass

        # More copy/paste from ebpub.db.views...
        # As an optimization, limit the NewsItems to those published in the
        # last few days.
        end_date = today()
        start_date = end_date - datetime.timedelta(days=settings.DEFAULT_DAYS)
        # Bug http://developer.openblockproject.org/ticket/77:
        # This is using pub_date, but Aggregates use item_date, so there's
        # a visible disjoint between number of items on the map and the item
        # count shown on the homepage and location detail page.
        newsitem_qs = newsitem_qs.filter(pub_date__gt=start_date-datetime.timedelta(days=1), pub_date__lt=end_date+datetime.timedelta(days=1)).select_related()
        if not has_staff_cookie(request):
            newsitem_qs = newsitem_qs.filter(schema__is_public=True)

        # Put a hard limit on the number of newsitems, and throw away
        # older items.
        newsitem_qs = newsitem_qs.select_related().order_by('-pub_date')
        newsitem_qs = newsitem_qs[:constants.NUM_NEWS_ITEMS_PLACE_DETAIL]

    # Done preparing the query; cache based on the raw SQL
    # to be sure we capture everything that matters.
    cache_key += hashlib.md5(str(newsitem_qs.query)).hexdigest()
    output = cache.get(cache_key, None)
    if output is None:
        # Re-sort by schema type.
        # This is an optimization for map_popups().
        # We can't do it in the qs because we want to first slice the qs
        # by date, and we can't call order_by() after a slice.
        newsitem_list = sorted(newsitem_qs, key=lambda ni: ni.schema.id)
        popup_list = map_popups(newsitem_list)

        features = {'type': 'FeatureCollection', 'features': []}
        for newsitem, popup_info in zip(newsitem_list, popup_list):
            if newsitem.location is None:
                # Can happen, see NewsItem docstring.
                # TODO: We should probably allow for newsitems that have a
                # location_object too?
                continue

            features['features'].append(
                {'type': 'Feature',
                 'geometry': {'type': 'Point',
                              'coordinates': [newsitem.location.centroid.x,
                                              newsitem.location.centroid.y],
                              },
                 'properties': {
                        'title': newsitem.title,
                        'id': popup_info[0],
                        'popup_html': popup_info[1],
                        'schema': popup_info[2],
                        }
                 })
        output = simplejson.dumps(features, indent=1)
        cache.set(cache_key, output, cache_seconds)

    response = HttpResponse(output, mimetype="application/javascript")
    patch_response_headers(response, cache_timeout=60 * 5)
    return response


@cache_page(60 * 60)
def place_kml(request, *args, **kwargs):
    place = url_to_place(*args, **kwargs)
    return render_to_kml('place.kml', {'place': place})
