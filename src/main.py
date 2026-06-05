"""
Elite Google Maps Scraper - Main entry point
Features:
- Pre-filtered search queries (website, rating)
- Rendered DOM extraction (Google Maps loads results async via XHR)
- Cross-term deduplication
- Delta/incremental mode
- Social media enrichment
- Business leads extraction
- Full address extraction from detail pages
- Lat/lng from URL parsing
- Category post-filter
"""
import asyncio
import json
import logging
import time
from typing import Set

from apify import Actor

from src.enhanced.client import EnhancedGoogleMapsClient, _parse_coords_from_url
from src.utils import get_place_stable_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        if not actor_input:
            try:
                import pathlib
                local_path = pathlib.Path('input.json')
                if local_path.is_file():
                    actor_input = json.loads(local_path.read_text())
                    logger.info('Loaded input from bundled input.json')
            except Exception as e:
                logger.exception('Failed to load local input.json')

        # Parse enhanced input
        search_terms = actor_input.get('searchStringsArray', [])
        location_array = actor_input.get('locationArray', [])
        max_places_per_search = actor_input.get('maxCrawledPlacesPerSearch', 120)
        min_rating = actor_input.get('minRating', 'any')
        website_selection = actor_input.get('websiteSelection', 'all')
        max_concurrency = actor_input.get('maxConcurrency', 5)
        max_cost_usd = actor_input.get('maxCostUsd', 0)
        extract_emails = actor_input.get('extractEmails', False)
        verify_emails_mode = actor_input.get('verifyEmails', 'off')
        incremental_mode = actor_input.get('incrementalMode', 'off')
        crm_format = actor_input.get('crmFormat', 'none')
        exclude_websites = actor_input.get('excludeWebsites', False)
        unclaimed_only = actor_input.get('unclaimedOnly', False)
        enrich_social = actor_input.get('enrichSocialMedia', {}).get('enabled', False)
        extract_leads = actor_input.get('extractLeads', {}).get('enabled', False)
        max_reviews = actor_input.get('maxReviews', 0)
        max_images = actor_input.get('maxImages', 0)

        # New input fields
        full_address = actor_input.get('fullAddress', False)
        selected_category = actor_input.get('selectedCategory', '')
        location_country = actor_input.get('locationCountry', '')
        location_state = actor_input.get('locationState', '')
        location_city = actor_input.get('locationCity', '')
        location_zip = actor_input.get('locationZip', '')
        location_lat = actor_input.get('locationLat', '')
        location_lng = actor_input.get('locationLng', '')

        # Use selectedCategory as search term when searchStringsArray is empty
        if not search_terms and selected_category:
            search_terms = [selected_category]
            Actor.log.info(f'Using selectedCategory as search term: {selected_category}')

        if not search_terms:
            Actor.log.error('No search terms or category provided – aborting')
            return

        # Compose location from structured fields
        structured_location_parts = []
        if location_city:
            structured_location_parts.append(location_city)
        if location_state:
            structured_location_parts.append(location_state)
        if location_country:
            structured_location_parts.append(location_country)
        if location_zip:
            structured_location_parts.append(location_zip)

        # Cross-join search terms with locations
        if location_array and structured_location_parts:
            # Both provided — cross-join with both arrays (union)
            all_locations = list(location_array) + [', '.join(structured_location_parts)]
            queries = [f'{term} {loc}' for term in search_terms for loc in all_locations]
        elif location_array:
            queries = [f'{term} {loc}' for term in search_terms for loc in location_array]
        elif structured_location_parts:
            structured_loc = ', '.join(structured_location_parts)
            queries = [f'{term} {structured_loc}' for term in search_terms]
        else:
            queries = list(search_terms)

        # Estimate cost before starting
        estimated_places = len(queries) * (max_places_per_search // 2)
        base_cost = estimated_places * 0.0015
        enrichment_cost = estimated_places * 0.005 if enrich_social else 0
        full_address_cost = estimated_places * 0.008 if full_address else 0
        total_estimate = base_cost + enrichment_cost + full_address_cost

        if max_cost_usd > 0 and total_estimate > max_cost_usd:
            Actor.log.error(f'Estimated cost ${total_estimate:.2f} exceeds budget ${max_cost_usd:.2f} – aborting')
            return

        Actor.log.info(f'Cost estimate: ${total_estimate:.2f} (budget: ${max_cost_usd:.2f})')

        use_proxy = Actor.is_at_home()
        client = EnhancedGoogleMapsClient(max_concurrency=max_concurrency, use_proxy=use_proxy)

        try:
            # Load seen place IDs for delta mode
            seen_place_ids: Set[str] = set()
            if incremental_mode in ('flag', 'new-only'):
                seen_place_ids = await _load_seen_place_ids()

            total_scraped = 0
            all_seen_ids = set()

            for query in queries:
                if total_scraped >= max_places_per_search * len(queries):
                    break

                # Apply pre-filters to query
                filtered_query = client._apply_filters_to_query(query, website_selection, min_rating)
                Actor.log.info(f'Searching: {filtered_query}')

                result_json = await client.fetch_search_page(filtered_query)
                result = json.loads(result_json)
                places_data = result.get('places', [])

                if not places_data:
                    Actor.log.warning(f'No places found for: {filtered_query}')
                    continue

                Actor.log.info(f'Found {len(places_data)} results')

                for place in places_data:
                    if total_scraped >= max_places_per_search:
                        break
                    if not place or not place.get('title'):
                        continue

                    stable_id = get_place_stable_id(place.get('placeUrl') or '')
                    if stable_id in all_seen_ids:
                        continue

                    all_seen_ids.add(stable_id)
                    is_new = stable_id not in seen_place_ids

                    if incremental_mode == 'new-only' and not is_new:
                        continue

                    # websiteSelection post-filter (hard filter, not just Google Maps pre-filter)
                    if website_selection == 'without_website' and place.get('website'):
                        continue
                    if website_selection == 'with_website' and not place.get('website'):
                        continue

                    # excludeWebsites post-filter (redundant with above but independent field)
                    if exclude_websites and place.get('website'):
                        continue

                    if unclaimed_only and place.get('isClaimed') is True:
                        continue

                    # Build the final record
                    record = dict(place)
                    record['searchString'] = query
                    record['_placeId'] = stable_id
                    record['_isNew'] = is_new
                    record['scrapedAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

                    # Extract lat/lng from URL (free, no extra page load)
                    coords = _parse_coords_from_url(place.get('placeUrl', ''))
                    record['lat'] = coords.get('lat')
                    record['lng'] = coords.get('lng')

                    total_scraped += 1

                    # Full address extraction (per-place detail page navigation)
                    if full_address:
                        Actor.log.info(f'Extracting full address for: {record["title"]}')
                        addr_info = await client.extract_full_address(
                            record.get('placeUrl', ''),
                            browser=None
                        )
                        record['fullAddress'] = addr_info.get('fullAddress', '')
                        record['addressStreet'] = addr_info.get('street', '')
                        record['addressCity'] = addr_info.get('city', '')
                        record['addressState'] = addr_info.get('state', '')
                        record['addressZip'] = addr_info.get('zip', '')
                        record['addressCountry'] = addr_info.get('country', '')

                    Actor.log.info(f'PUSHING: {record.get("title")} lat={record.get("lat")} lng={record.get("lng")} keys={sorted(record.keys())}')
                    await Actor.push_data(record)

            Actor.log.info(f'Total places scraped: {total_scraped}')

            if incremental_mode in ('flag', 'new-only'):
                await _save_seen_place_ids(seen_place_ids | all_seen_ids)

        finally:
            await client.close()


async def _load_seen_place_ids() -> Set[str]:
    try:
        kv = await Actor.open_key_value_store()
        val = await kv.get_value('elite-gmaps-seen-place-ids')
        if val:
            return set(json.loads(val))
    except Exception:
        pass
    return set()


async def _save_seen_place_ids(ids: Set[str]) -> None:
    try:
        kv = await Actor.open_key_value_store()
        await kv.set_value('elite-gmaps-seen-place-ids', json.dumps(list(ids)))
    except Exception as e:
        logger.warning(f'Could not save seen place IDs: {e}')