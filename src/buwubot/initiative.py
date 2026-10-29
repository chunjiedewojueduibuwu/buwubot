"""Shared authorization and freshness checks for optional owner-only private contact."""


def owner_target(cfg):
    if (not cfg.owner_initiative_enabled or not cfg.runtime_enabled or not cfg.qq_enabled
            or not cfg.owner_private_enabled or not cfg.owner_id or cfg.owner_id == cfg.self_id):
        return None
    return 'private:' + cfg.owner_id


def owner_input(store, cfg):
    rows = store.rows("SELECT id,time FROM messages WHERE group_id=? AND user_id=? AND recalled=0 "
        "AND id NOT IN (SELECT alias_id FROM message_aliases) ORDER BY time DESC,"
        "COALESCE(json_extract(payload,'$.source_order'),received_order) DESC,received_order DESC LIMIT 1",
        ('private:' + cfg.owner_id,cfg.owner_id))
    return dict(rows[0]) if rows else None


def initiative_valid(store, cfg, payload, now, job_id=''):
    target = owner_target(cfg)
    anchor = owner_input(store,cfg) if target else None
    if (not target or payload.get('group_id') != target or payload.get('persona_id') != cfg.persona_id
            or not anchor or payload.get('owner_anchor') != anchor['id']
            or now - payload.get('opportunity_time',0) > cfg.owner_initiative_interval_minutes * 60):
        return False
    return not store.rows("SELECT j.id FROM jobs j JOIN outgoing o ON o.job_id=j.id "
        "WHERE j.kind='initiative' AND j.id<>? AND o.group_id=? "
        "AND json_extract(j.payload,'$.persona_id')=? AND json_extract(j.payload,'$.owner_anchor')=? "
        "AND (o.status IN ('inflight','uncertain','unconfirmed') OR (o.status='sent' AND o.started_at>=?)) LIMIT 1",
        (job_id,target,cfg.persona_id,anchor['id'],now - cfg.owner_initiative_unanswered_hours * 3600))
