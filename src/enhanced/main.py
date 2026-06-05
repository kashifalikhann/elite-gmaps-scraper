"""
Elite Google Maps Scraper - Main entry point
Features:
- Pre-filtered search queries (website, rating)
- Rendered DOM extraction (Google Maps loads results async via XHR)
- Cross-term deduplication
- Delta/incremental mode
- Social media enrichment
- Business leads extraction
"""
import asyncio
import json
import logging
import time
from typing import Set

from apify import Actor

from src.enhanced.client import EnhancedGoogleMapsClient
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

        if not search_terms:
            Actor.log.error('No search terms provided – aborting')
            return

        estimated_places = len(search_terms) * (max_places_per_search // 2)
        base_cost = estimated_places * 0.0015
        enrichment_cost = estimated_places * 0.005 if enrich_social else 0
        total_estimate = base_cost + enrichment_cost

        if max_cost_usd > 0 and total_estimate > max_cost_usd:
            Actor.log.error(f'Estimated cost ${total_estimate:.2f} exceeds budget ${max_cost_usd:.2f} – aborting')
            return

        Actor.log.info(f'Cost estimate: ${total_estimate:.2f} (budget: ${max_cost_usd:.2f})')

        use_proxy = Actor.is_at_home()
        client = EnhancedGoogleMapsClient(max_concurrency=max_concurrency, use_proxy=use_proxy)

        try:
            seen_place_ids: Set[str] = set()
            if incremental_mode in ('flag', 'new-only'):
                seen_place_ids = await _load_seen_place_ids()

            total_scraped = 0
            all_seen_ids = set()

            for query in search_terms:
                if total_scraped >= max_places_per_search * len(search_terms):
                    break

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

                    if exclude_websites and place.get('website'):
                        continue

                    if unclaimed_only and place.get('isClaimed') is True:
                        continue

                    place['searchString'] = query
                    place['_placeId'] = stable_id
                    place['_isNew'] = is_new
                    place['scrapedAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

                    total_scraped += 1
                    await Actor.push_data(place)

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