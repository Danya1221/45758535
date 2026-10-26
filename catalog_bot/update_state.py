"""Persist group update cursors independently of the cached group picker.

Telethon 1.45 only loads saved channel cursors at connect with catch_up=True.
This application deliberately starts without account-wide history replay, but
still needs the channel cursors to recover a live UpdateChannelTooLong. Keep the
small SDK-specific integration here and restore it only after authorization has
been checked, with a matching persisted account marker.
"""
from telethon import types, utils
from telethon._updates.session import Entity, EntityType


async def restore_channel_updates(client, account_id: int) -> int:
    """Restore cached channel cursors locally; perform no Telegram requests.

Never reset the current account state or replace a channel cursor that Telethon
has already learned in this connection. The stored self marker guards against
reusing update cursors when a different account logs in to the same session.
"""
    box = getattr(client, '_message_box', None)
    cache = getattr(client, '_mb_entity_cache', None)
    session = getattr(client, 'session', None)
    if box is None or cache is None or session is None or not session.auth_key:
        return 0
    states = list(await utils.maybe_async(session.get_update_states()))
    try:
        marker = await utils.maybe_async(session.get_input_entity(0))
    except ValueError:
        marker = None
    valid_marker = isinstance(marker, types.InputPeerUser) and marker.user_id == 0
    if not valid_marker or marker.access_hash != account_id:
        # Telethon does not remove old update_state rows when a session key is
        # replaced. Simply skipping restore is insufficient: its next save
        # writes the new account marker beside those old channel cursors, and
        # a second restart would then trust them. Zero the untrusted cursors
        # using the public session API, including stale in-memory entries that
        # its next checkpoint could otherwise write back.
        stale = {cid for cid, _ in states if cid > 0}
        if valid_marker:
            stale.update(cid for cid in box.map if isinstance(cid, int))
        for cid in stale:
            await utils.maybe_async(session.set_update_state(cid, types.updates.State(
                pts=0, qts=0, date=box.date, seq=0, unread_count=0)))
            box.map.pop(cid, None)
            box.possible_gaps.pop(cid, None)
            box.getting_diff_for.discard(cid)
        if box.next_deadline in stale:
            box.next_deadline = min(box.map, key=lambda entry: box.map[entry].deadline) if box.map else None
        if stale:
            cache.retain(lambda cid: cid not in stale)
            await utils.maybe_async(session.save())
        return 0

    restored = 0
    for channel_id, state in states:
        if channel_id <= 0 or state.pts <= 0:
            continue
        try:
            peer = await utils.maybe_async(session.get_input_entity(types.PeerChannel(channel_id)))
        except ValueError:
            continue  # A cursor without an access hash cannot recover gaps.
        if not isinstance(peer, types.InputPeerChannel):
            continue
        already_known = channel_id in box.map
        box.try_set_channel_state(channel_id, state.pts)
        if cache.get(channel_id) is None:
            cache.put(Entity(EntityType.CHANNEL, channel_id, peer.access_hash))
        restored += not already_known
    return restored


async def checkpoint_update_state(client) -> bool:
    """Flush SDK cursors to the session before an early deployment/restart.

Group discovery populates Telethon's in-memory cursors. Waiting for its periodic
keepalive to save them can lose that state while our own group cache survives.
The SDK helper also saves its own account marker, so use its format unchanged.
"""
    box = getattr(client, '_message_box', None)
    save = getattr(client, '_save_states_and_entities', None)
    session = getattr(client, 'session', None)
    if box is None or save is None or session is None or box.is_empty():
        return False
    await save()
    await utils.maybe_async(session.save())
    return True
