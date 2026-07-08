"""REST API routes for the Emby-Trakt Watch Party standalone."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.schema import User
from app.utils.database import get_db
from app.utils.trakt_client import TraktClient
from app.utils.emby_client import EmbyClient
from app.utils.redis_cache import get_redis

from app.security.auth import get_current_user, require_user_ownership, issue_tokens

from app.services.watch_party.service import WatchPartyService
from app.utils.database import async_session as async_session_ctx


async def _first_emby_user_id() -> str | None:
    """Return the emby_user_id of the first linked user (for user-scoped queries)."""
    async with async_session_ctx() as db:
        user = (await db.execute(
            select(User).where(User.trakt_access_token.isnot(None)).order_by(User.id)
        )).scalars().first()
    return user.emby_user_id if user else None


watch_party_svc = WatchPartyService()

log = structlog.get_logger()

router = APIRouter()


# ═══════════════════════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/health")
async def health():
    return {
        "status": "ok",
        "app": "emby-trakt-watch-party",
        "features": {
            "watch_party": True,
            "trakt_scrobble": True,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# Auth — Trakt device-code OAuth
# ═══════════════════════════════════════════════════════════════════════════

class LinkRequest(BaseModel):
    emby_user_id: str
    emby_username: str = ""


class LinkPollRequest(BaseModel):
    emby_user_id: str
    device_code: str


@router.post("/auth/trakt/device-code")
async def trakt_device_code(body: LinkRequest, db: AsyncSession = Depends(get_db)):
    """Start Trakt device-code flow.  Returns user_code + verification_url."""
    user = (await db.execute(
        select(User).where(User.emby_user_id == body.emby_user_id)
    )).scalar_one_or_none()

    if not user:
        user = User(emby_user_id=body.emby_user_id, emby_username=body.emby_username)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    trakt = TraktClient()
    try:
        result = await trakt.get_device_code()
    finally:
        await trakt.close()

    return {
        "user_code": result["user_code"],
        "verification_url": result["verification_url"],
        "device_code": result["device_code"],
        "expires_in": result["expires_in"],
        "interval": result["interval"],
    }


@router.post("/auth/trakt/poll")
async def trakt_poll(body: LinkPollRequest, db: AsyncSession = Depends(get_db)):
    """Poll for completed Trakt authorisation."""
    trakt = TraktClient()
    try:
        token_data = await trakt.poll_device_token(body.device_code)
    finally:
        await trakt.close()

    if not token_data:
        return {"status": "pending"}

    user = (await db.execute(
        select(User).where(User.emby_user_id == body.emby_user_id)
    )).scalar_one_or_none()

    if not user:
        raise HTTPException(404, "User not found — call device-code first")

    user.trakt_access_token = token_data["access_token"]
    user.trakt_refresh_token = token_data["refresh_token"]
    user.trakt_token_expires = datetime.utcnow() + timedelta(seconds=token_data.get("expires_in", 7776000))

    authed = TraktClient(access_token=token_data["access_token"])
    try:
        me = await authed.get_me()
        user.trakt_username = me.get("user", {}).get("username", "")
    finally:
        await authed.close()

    await db.commit()

    tokens = await issue_tokens(user)

    return {
        "status": "linked",
        "trakt_username": user.trakt_username,
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "token_type": tokens.token_type,
        "expires_in": tokens.expires_in,
    }


@router.get("/auth/users")
async def list_users(db: AsyncSession = Depends(get_db)):
    users = (await db.execute(select(User))).scalars().all()
    now = datetime.utcnow()
    result = []
    for u in users:
        expires = u.trakt_token_expires
        token_info = {}
        if expires:
            delta = expires - now
            total_secs = int(delta.total_seconds())
            if total_secs > 0:
                days = total_secs // 86400
                token_info = {
                    "token_expires": expires.isoformat(),
                    "token_days_left": days,
                    "token_status": "ok" if days > 7 else "expiring_soon" if days > 0 else "expired",
                }
            else:
                token_info = {
                    "token_expires": expires.isoformat(),
                    "token_days_left": 0,
                    "token_status": "expired",
                }
        result.append({
            "id": u.id,
            "emby_user_id": u.emby_user_id,
            "emby_username": u.emby_username,
            "trakt_username": u.trakt_username,
            "linked": bool(u.trakt_access_token),
            **token_info,
        })
    return result


@router.get("/auth/emby-users")
async def list_all_emby_users(db: AsyncSession = Depends(get_db)):
    """Return all Emby server users, auto-creating DB records for any missing."""
    emby = EmbyClient()
    try:
        emby_users = await emby.get_users()
    except Exception as e:
        raise HTTPException(502, f"Could not reach Emby server: {e}")
    finally:
        await emby.close()

    existing = (await db.execute(select(User))).scalars().all()
    by_emby_id = {u.emby_user_id: u for u in existing}

    created = 0
    for eu in emby_users:
        eid = eu.get("Id", "")
        if not eid:
            continue
        if eid not in by_emby_id:
            new_user = User(
                emby_user_id=eid,
                emby_username=eu.get("Name", ""),
            )
            db.add(new_user)
            by_emby_id[eid] = new_user
            created += 1
        else:
            db_user = by_emby_id[eid]
            emby_name = eu.get("Name", "")
            if emby_name and db_user.emby_username != emby_name:
                db_user.emby_username = emby_name

    if created:
        await db.commit()
        for u in by_emby_id.values():
            await db.refresh(u)

    now = datetime.utcnow()
    result = []
    for u in by_emby_id.values():
        token_info = {}
        if u.trakt_token_expires:
            delta = u.trakt_token_expires - now
            days = max(0, int(delta.total_seconds()) // 86400)
            token_info = {
                "token_status": "ok" if days > 7 else "expiring_soon" if days > 0 else "expired",
                "token_days_left": days,
            }
        result.append({
            "id": u.id,
            "emby_user_id": u.emby_user_id,
            "emby_username": u.emby_username,
            "trakt_username": u.trakt_username,
            "linked": bool(u.trakt_access_token),
            **token_info,
        })

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Watch Party
# ═══════════════════════════════════════════════════════════════════════════

class CreatePartyRequest(BaseModel):
    host_user_id: int
    emby_item_id: str


class JoinPartyRequest(BaseModel):
    code: str
    user_id: int


@router.post("/party/create")
async def create_party(body: CreatePartyRequest):
    try:
        return await watch_party_svc.create_party(body.host_user_id, body.emby_item_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/party/join")
async def join_party(body: JoinPartyRequest):
    result = await watch_party_svc.join_party(body.code, body.user_id)
    if not result:
        raise HTTPException(404, "Party not found or has ended")
    return result


@router.post("/party/{code}/end")
async def end_party(code: str):
    await watch_party_svc.end_party(code)
    return {"status": "ended"}


@router.post("/party/{code}/start")
async def start_party_playback(code: str):
    """Start playback on all participants' Emby sessions simultaneously."""
    return await watch_party_svc.start_playback(code)


@router.get("/party/{code}/sessions")
async def list_party_sessions(code: str):
    """List active Emby sessions for party participants (device picker)."""
    return await watch_party_svc.list_sessions_for_party(code)


@router.post("/party/{code}/start-selected")
async def start_selected_playback(code: str, payload: dict):
    """Start playback on specific devices only."""
    session_ids = payload.get("session_ids", [])
    item_id = payload.get("emby_item_id")
    if not session_ids:
        raise HTTPException(400, "No sessions selected")
    return await watch_party_svc.start_playback_on_sessions(code, session_ids, item_id)


@router.post("/party/{code}/pause")
async def pause_party_playback(code: str):
    """Toggle pause/play on all participants' Emby sessions."""
    return await watch_party_svc.pause_all(code)


@router.post("/party/{code}/seek")
async def seek_party_playback(code: str, payload: dict):
    """Seek all participants to a specific position."""
    position_ticks = payload.get("position_ticks", 0)
    return await watch_party_svc.seek_all(code, position_ticks)


@router.get("/party/{code}")
async def get_party(code: str):
    result = await watch_party_svc.get_party(code)
    if not result:
        raise HTTPException(404, "Party not found")
    return result


@router.get("/parties")
async def list_parties():
    return await watch_party_svc.list_active_parties()


@router.get("/parties/recent")
async def list_recent_parties(limit: int = Query(10, ge=1, le=50)):
    """Return recently ended parties for the watch party lobby."""
    return await watch_party_svc.list_recent_parties(limit)


# ═══════════════════════════════════════════════════════════════════════════
# Emby Webhook receiver (scrobble-only — no queue feedback)
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/webhook/emby")
@router.post("/")
async def emby_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Receive Emby webhooks for Trakt scrobble/history sync.

    Also registered at POST / as a fallback since Emby may be configured
    with just the root URL.
    """
    import json as _json
    from app.utils.database import async_session

    content_type = request.headers.get("content-type", "")
    payload = {}

    try:
        if "application/json" in content_type:
            payload = await request.json()
        elif "form" in content_type or "multipart" in content_type:
            form = await request.form()
            raw = form.get("data", "{}")
            payload = _json.loads(raw) if isinstance(raw, str) else {}
        else:
            body = await request.body()
            if body:
                try:
                    payload = _json.loads(body)
                except (ValueError, _json.JSONDecodeError):
                    payload = {}
    except Exception:
        return {"status": "ignored", "reason": "unparseable_body"}

    if not payload:
        return {"status": "ignored", "reason": "empty_payload"}

    event_type = payload.get("Event", "") or payload.get("EventType", "")
    item_data = payload.get("Item", {})
    user_data = payload.get("User", {})
    session_data = payload.get("Session", {})

    item_name = item_data.get("Name", "")
    item_type_raw = item_data.get("Type", "")
    emby_item_id = item_data.get("Id", "")
    emby_user_id = user_data.get("Id", "")
    emby_username = user_data.get("Name", "")

    if not emby_item_id or not emby_user_id:
        return {"status": "ok", "event": event_type, "note": "no item/user data"}

    user = (await db.execute(
        select(User).where(User.emby_user_id == emby_user_id)
    )).scalar_one_or_none()

    if not user:
        await _activity_log(
            f"Webhook ignored: unknown Emby user {emby_username} ({emby_user_id})",
            category="webhook",
        )
        return {"status": "ignored", "reason": "unknown_user"}

    trakt_synced = False

    # -- Helpers ---------------------------------------------------------------

    async def _get_trakt_client():
        async def _on_refresh(access, refresh, expires):
            async with async_session() as _db:
                u = await _db.get(User, user.id)
                u.trakt_access_token = access
                u.trakt_refresh_token = refresh
                u.trakt_token_expires = expires
                await _db.commit()

        return TraktClient(
            access_token=user.trakt_access_token,
            refresh_token=user.trakt_refresh_token,
            token_expires=user.trakt_token_expires,
            token_refresh_callback=_on_refresh,
        )

    def _build_scrobble_payload():
        provider_ids = item_data.get("ProviderIds", {})
        trakt_ids = {}
        if provider_ids.get("Imdb"):
            trakt_ids["imdb"] = provider_ids["Imdb"]
        if provider_ids.get("Tmdb"):
            trakt_ids["tmdb"] = int(provider_ids["Tmdb"])
        if provider_ids.get("Tvdb"):
            trakt_ids["tvdb"] = int(provider_ids["Tvdb"])
        if not trakt_ids:
            return None

        if item_type_raw == "Movie":
            return {"movie": {"ids": trakt_ids}}
        elif item_type_raw == "Episode":
            series_ids = {}
            series_provider = item_data.get("SeriesProviderIds", {})
            if series_provider.get("Imdb"):
                series_ids["imdb"] = series_provider["Imdb"]
            if series_provider.get("Tmdb"):
                series_ids["tmdb"] = int(series_provider["Tmdb"])
            if series_provider.get("Tvdb"):
                series_ids["tvdb"] = int(series_provider["Tvdb"])
            return {
                "show": {"ids": series_ids or trakt_ids},
                "episode": {
                    "season": item_data.get("ParentIndexNumber", 1),
                    "number": item_data.get("IndexNumber", 1),
                },
            }
        return None

    def _get_position_ticks():
        pos = session_data.get("PlayState", {}).get("PositionTicks", 0)
        if pos:
            return pos
        pos = payload.get("PlaybackPositionTicks", 0)
        if pos:
            return pos
        return payload.get("PlaybackInfo", {}).get("PositionTicks", 0)

    def _calc_progress():
        pos = _get_position_ticks()
        duration = item_data.get("RunTimeTicks", 0)
        if duration > 0 and pos > 0:
            return min(99.9, max(1.0, pos / duration * 100))
        return 1.0

    # ── Match events ─────────────────────────────────────────────────────────
    event_lower = event_type.lower()

    is_play_start = event_lower in ("playback.start", "playbackstart")
    is_play_stop = event_lower in ("playback.stop", "playbackstop")
    is_play_pause = event_lower in ("playback.pause", "playbackpause")
    is_play_unpause = event_lower in ("playback.unpause", "playbackunpause",
                                       "playback.resume", "playbackresume")
    is_mark_played = event_lower in ("item.markplayed", "item.markedplayed",
                                      "itemmarkplayed", "itemmarkedplayed")
    is_watched = is_play_stop or is_mark_played

    # ── playback.start → Trakt scrobble/start ───────────────────────────────
    if is_play_start:
        if user.trakt_access_token:
            try:
                trakt = await _get_trakt_client()
                scrobble = _build_scrobble_payload()
                if scrobble:
                    progress = _calc_progress()
                    await trakt.scrobble_start(scrobble, progress=progress)
                    trakt_synced = True
                    await _activity_log(f"▶ Trakt watching: {item_name}", category="trakt")
            except Exception as e:
                log.warning("webhook.trakt_scrobble_start_failed", error=str(e))
                await _activity_log(f"⚠ Trakt start failed: {item_name} — {str(e)[:80]}", category="trakt")
        return {"status": "received", "event": event_type, "trakt_synced": trakt_synced}

    # ── playback.pause → Trakt scrobble/pause ───────────────────────────────
    if is_play_pause:
        if user.trakt_access_token:
            progress = _calc_progress()
            if progress > 80:
                await _activity_log(
                    f"⏸ Paused near end: {item_name} ({progress:.0f}%) — skipped scrobble, stop will sync",
                    category="trakt",
                )
            else:
                try:
                    trakt = await _get_trakt_client()
                    scrobble = _build_scrobble_payload()
                    if scrobble:
                        await trakt.scrobble_pause(scrobble, progress=progress)
                        trakt_synced = True
                        pos_secs = _get_position_ticks() // 10000000
                        mm, ss = divmod(pos_secs, 60)
                        await _activity_log(
                            f"⏸ Trakt paused: {item_name} at {mm}:{ss:02d} ({progress:.0f}%)",
                            category="trakt",
                        )
                except Exception as e:
                    err_str = str(e)
                    if "422" in err_str:
                        await _activity_log(
                            f"⏸ Pause skipped by Trakt: {item_name} ({progress:.0f}%) — will sync on stop",
                            category="trakt",
                        )
                    else:
                        log.warning("webhook.trakt_scrobble_pause_failed", error=err_str)
                        await _activity_log(f"⚠ Trakt pause failed: {item_name} — {err_str[:80]}", category="trakt")
        return {"status": "received", "event": event_type, "trakt_synced": trakt_synced}

    # ── playback.unpause → Trakt scrobble/start (resume) ────────────────────
    if is_play_unpause:
        if user.trakt_access_token:
            try:
                trakt = await _get_trakt_client()
                scrobble = _build_scrobble_payload()
                if scrobble:
                    progress = _calc_progress()
                    await trakt.scrobble_start(scrobble, progress=progress)
                    trakt_synced = True
                    pos_secs = _get_position_ticks() // 10000000
                    mm, ss = divmod(pos_secs, 60)
                    await _activity_log(
                        f"▶ Trakt resumed: {item_name} at {mm}:{ss:02d} ({progress:.0f}%)",
                        category="trakt",
                    )
            except Exception as e:
                log.warning("webhook.trakt_scrobble_resume_failed", error=str(e))
                await _activity_log(f"⚠ Trakt resume failed: {item_name} — {str(e)[:80]}", category="trakt")
        return {"status": "received", "event": event_type, "trakt_synced": trakt_synced}

    # ── playback.stop / item.markplayed → Trakt watch history ───────────────
    if is_watched:
        await _activity_log(
            f"⏹ Stopped: {item_name} ({item_type_raw}) — {emby_username}",
            category="playback",
        )

        if user.trakt_access_token:
            try:
                trakt = await _get_trakt_client()

                provider_ids = item_data.get("ProviderIds", {})
                trakt_ids = {}
                if provider_ids.get("Imdb"):
                    trakt_ids["imdb"] = provider_ids["Imdb"]
                if provider_ids.get("Tmdb"):
                    trakt_ids["tmdb"] = int(provider_ids["Tmdb"])
                if provider_ids.get("Tvdb"):
                    trakt_ids["tvdb"] = int(provider_ids["Tvdb"])

                if trakt_ids:
                    watched_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

                    if item_type_raw in ("Movie",):
                        history_item = {"ids": trakt_ids, "watched_at": watched_at}
                        await trakt.add_to_history([history_item])
                        trakt_synced = True
                        log.info("webhook.trakt_history_synced", type="movie", ids=trakt_ids, user=user.id)
                        await _activity_log(f"✓ Synced to Trakt: {item_name} (movie)", category="trakt")

                    elif item_type_raw in ("Episode",):
                        series_ids = {}
                        series_provider = item_data.get("SeriesProviderIds", {})
                        if series_provider.get("Imdb"):
                            series_ids["imdb"] = series_provider["Imdb"]
                        if series_provider.get("Tmdb"):
                            series_ids["tmdb"] = int(series_provider["Tmdb"])
                        if series_provider.get("Tvdb"):
                            series_ids["tvdb"] = int(series_provider["Tvdb"])

                        episode = {"watched_at": watched_at, "ids": trakt_ids}
                        season_num = item_data.get("ParentIndexNumber")
                        episode_num = item_data.get("IndexNumber")
                        if season_num is not None:
                            episode["season"] = season_num
                        if episode_num is not None:
                            episode["number"] = episode_num

                        show_item = {
                            "_type": "show",
                            "ids": series_ids or trakt_ids,
                            "seasons": [{"number": season_num or 1, "episodes": [episode]}],
                        }
                        await trakt.add_to_history([show_item])
                        trakt_synced = True
                        log.info("webhook.trakt_history_synced", type="episode", ids=trakt_ids, user=user.id)
                        await _activity_log(
                            f"✓ Synced to Trakt: {item_name} S{season_num or '?'}E{episode_num or '?'}",
                            category="trakt",
                        )
                    else:
                        await _activity_log(
                            f"Skipped Trakt sync: {item_name} — unsupported type '{item_type_raw}'",
                            category="trakt",
                        )
                else:
                    await _activity_log(
                        f"Skipped Trakt sync: {item_name} — no provider IDs (IMDB/TMDB/TVDB)",
                        category="trakt",
                    )

            except Exception as e:
                log.error("webhook.trakt_sync_failed", error=str(e), user=user.id)
                await _activity_log(f"✗ Trakt sync failed: {item_name} — {str(e)[:80]}", category="trakt")
        else:
            await _activity_log(
                f"Skipped Trakt sync: {item_name} — user has no Trakt token",
                category="trakt",
            )

    if not is_watched and not is_play_start and not is_play_pause and not is_play_unpause:
        await _activity_log(f"📡 Unhandled webhook: {event_type} — {item_name}", category="webhook")

    return {"status": "received", "event": event_type, "trakt_synced": trakt_synced}


# -- Activity log (Redis-backed, last 100 entries) ---------------------------

async def _activity_log(message: str, category: str = "general"):
    """Append an entry to the activity log in Redis."""
    import json as _json
    try:
        r = await get_redis()
        entry = _json.dumps({
            "ts": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "cat": category,
            "msg": message,
        })
        await r.lpush("activity_log", entry)
        await r.ltrim("activity_log", 0, 99)
    except Exception:
        pass


@router.get("/api/activity")
async def get_activity(
    limit: int = Query(default=30, le=100),
    category: str = Query(default=None),
):
    """Return recent activity log entries, optionally filtered by category."""
    import json as _json
    r = await get_redis()
    fetch_count = 99 if category else limit - 1
    raw = await r.lrange("activity_log", 0, fetch_count)
    entries = []
    for item in raw:
        try:
            entry = _json.loads(item)
            if category and entry.get("cat") != category:
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        except Exception:
            pass
    return entries


# ═══════════════════════════════════════════════════════════════════════════
# Library Search (used by the watch party item picker)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/library/search")
async def library_search(q: str = Query(..., min_length=2, max_length=100)):
    """Search Emby library by title.

    Returns resolution/quality info so users can distinguish 1080p from 4K
    when duplicates exist.
    """
    emby = EmbyClient()
    try:
        uid = await _first_emby_user_id()
        resp = await emby.get_items(
            user_id=uid,
            search_term=q,
            item_type=None,
            fields="ProviderIds,Genres,Overview,People,Studios,RunTimeTicks,MediaSources",
            limit=20,
        )
    finally:
        await emby.close()
    results = []
    for it in resp.get("Items", []):
        if it.get("Type") not in ("Movie", "Series", "Episode"):
            continue

        quality = ""
        media_sources = it.get("MediaSources") or []
        if media_sources:
            ms = media_sources[0]
            for stream in ms.get("MediaStreams", []):
                if stream.get("Type") == "Video":
                    w = stream.get("Width", 0)
                    h = stream.get("Height", 0)
                    if w >= 3840 or h >= 2160:
                        quality = "4K"
                    elif w >= 1920 or h >= 1080:
                        quality = "1080p"
                    elif w >= 1280 or h >= 720:
                        quality = "720p"
                    elif w > 0:
                        quality = f"{h}p"
                    if stream.get("VideoRangeType") in ("HDR10", "HDR10Plus", "DolbyVision", "HLG"):
                        quality += " HDR"
                    elif stream.get("VideoRange") == "HDR":
                        quality += " HDR"
                    break
            container = ms.get("Container", "")
            if container:
                quality += f" ({container})" if quality else container

        results.append({
            "id": it.get("Id"),
            "title": it.get("Name"),
            "year": it.get("ProductionYear"),
            "type": it.get("Type"),
            "quality": quality,
        })

    return results[:15]


# ═══════════════════════════════════════════════════════════════════════════
# HTML Pages
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/watch-party", response_class=HTMLResponse)
async def get_watch_party_page(code: str = None):
    """Serve the watch party chat page."""
    try:
        with open("frontend/templates/watch_party.html", "r") as f:
            html = f.read()
        if code:
            html = html.replace("const partyCode = null;", f"const partyCode = '{code}';")
        return html
    except FileNotFoundError:
        return "<h1>Page not found</h1>"


@router.get("/settings", response_class=HTMLResponse)
async def get_settings_page():
    """Serve the settings configuration page."""
    try:
        with open("frontend/templates/settings.html", "r") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>Page not found</h1>"


# ═══════════════════════════════════════════════════════════════════════════
# Settings API (simplified)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/settings")
async def read_settings():
    """Read current settings from environment."""
    return {
        "trakt_client_id": os.getenv("TRAKT_CLIENT_ID", "")[:8] + "****" if os.getenv("TRAKT_CLIENT_ID") else "",
        "trakt_client_secret": os.getenv("TRAKT_CLIENT_SECRET", "")[:8] + "****" if os.getenv("TRAKT_CLIENT_SECRET") else "",
        "emby_url": os.getenv("EMBY_URL", ""),
        "emby_api_key": os.getenv("EMBY_API_KEY", "")[:8] + "****" if os.getenv("EMBY_API_KEY") else "",
    }


class TestConnectionRequest(BaseModel):
    service: str


@router.post("/api/settings/test-connection")
async def test_connection(body: TestConnectionRequest):
    """Test Trakt or Emby connection."""
    if body.service == "trakt":
        trakt = TraktClient()
        try:
            await trakt.get_trending(kind="shows")
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        finally:
            await trakt.close()
    elif body.service == "emby":
        emby = EmbyClient()
        try:
            await emby.get_system_info()
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        finally:
            await emby.close()
    return {"status": "error", "message": f"Unknown service: {body.service}"}


@router.post("/api/settings/reset-oauth")
async def reset_oauth(db: AsyncSession = Depends(get_db)):
    """Clear all stored Trakt OAuth tokens."""
    users = (await db.execute(select(User))).scalars().all()
    for u in users:
        u.trakt_access_token = None
        u.trakt_refresh_token = None
        u.trakt_token_expires = None
    await db.commit()
    return {"status": "ok", "message": f"OAuth tokens cleared for {len(users)} user(s)."}


@router.post("/api/settings/factory-reset")
async def factory_reset(db: AsyncSession = Depends(get_db)):
    """Delete all users and data."""
    users = (await db.execute(select(User))).scalars().all()
    count = len(users)
    for u in users:
        await db.delete(u)
    await db.commit()
    return {"status": "ok", "message": f"Factory reset complete. Removed {count} user(s)."}


# ═══════════════════════════════════════════════════════════════════════════
# Database Backup / Restore
# ═══════════════════════════════════════════════════════════════════════════

def _parse_db_url(url: str) -> tuple[str, str, str, str]:
    from urllib.parse import urlparse, unquote
    parsed = urlparse(url)
    user = unquote(parsed.username or "embytrakt")
    password = unquote(parsed.password or "")
    host = parsed.hostname or "postgres"
    dbname = (parsed.path or "/embytrakt").lstrip("/")
    return user, password, host, dbname


@router.post("/api/db/backup")
async def create_db_backup():
    """Create a pg_dump backup."""
    import subprocess, uuid

    backup_dir = "/app/cache/backups"
    os.makedirs(backup_dir, exist_ok=True)
    backup_id = uuid.uuid4().hex[:12]
    filename = f"emby-trakt-backup-{backup_id}.sql"
    filepath = os.path.join(backup_dir, filename)

    db_url = os.environ.get("DATABASE_URL", "")
    db_user, db_pass, db_host, db_name = _parse_db_url(db_url)

    env = {**os.environ, "PGPASSWORD": db_pass}
    try:
        result = subprocess.run(
            ["pg_dump", "-h", db_host, "-U", db_user, "-d", db_name, "-f", filepath],
            capture_output=True, text=True, env=env, timeout=120,
        )
    except FileNotFoundError:
        return {"status": "error", "reason": "pg_dump not found"}

    if result.returncode != 0:
        return {"status": "error", "reason": result.stderr[:300]}

    return {"status": "ok", "backup_id": backup_id, "filename": filename, "size_bytes": os.path.getsize(filepath)}


@router.get("/api/db/backup/{backup_id}")
async def download_db_backup(backup_id: str):
    from fastapi.responses import FileResponse
    filepath = f"/app/cache/backups/emby-trakt-backup-{backup_id}.sql"
    if not os.path.isfile(filepath):
        raise HTTPException(404, "Backup not found")
    return FileResponse(filepath, media_type="application/sql", filename=os.path.basename(filepath))


@router.post("/api/db/restore")
async def restore_db_backup(request: Request):
    """Restore database from uploaded .sql backup."""
    import subprocess

    form = await request.form()
    upload = form.get("file")
    if not upload:
        raise HTTPException(400, "No file uploaded")

    restore_path = "/app/cache/backups/restore_upload.sql"
    os.makedirs("/app/cache/backups", exist_ok=True)
    contents = await upload.read()
    with open(restore_path, "wb") as f:
        f.write(contents)

    db_url = os.environ.get("DATABASE_URL", "")
    db_user, db_pass, db_host, db_name = _parse_db_url(db_url)

    env = {**os.environ, "PGPASSWORD": db_pass}
    try:
        result = subprocess.run(
            ["psql", "-h", db_host, "-U", db_user, "-d", db_name, "-f", restore_path],
            capture_output=True, text=True, env=env, timeout=120,
        )
    except FileNotFoundError:
        os.remove(restore_path)
        return {"status": "error", "reason": "psql not found"}

    os.remove(restore_path)

    if result.returncode != 0:
        return {"status": "error", "reason": result.stderr[:300]}

    return {"status": "ok", "message": "Database restored. Restart container for changes to take full effect."}
