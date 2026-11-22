"""Source records and completion rules for curator-release."""

CURATOR_RELEASE = {
    'curator-release': {
        'title': '馆长升级与文物放行',
        'reference_label': '馆长处置或巡检凭证号',
        'outcomes': {
            'released': ('馆长响应与最新巡检均通过', ('cleared',)),
            'hold': ('文物暂缓放行', None),
        },
        'sql': """
SELECT e.id::text AS source_key,CONCAT(c.artifact_code,' · 告警 ',e.id) AS subject,
       CONCAT(c.artifact_title,' · ',e.severity,' · 最近巡检 ',COALESCE(k.result,'尚无记录')) AS source_summary,
       CASE WHEN k.result='pass' AND EXISTS (SELECT 1 FROM duty_actions d WHERE d.event_id=e.id
               AND d.action_type='call_curator' AND d.status='completed') THEN 'cleared' ELSE 'awaiting' END AS readiness
FROM anomaly_events e JOIN display_cases c ON c.id=e.case_id
LEFT JOIN LATERAL (SELECT result FROM artifact_checks WHERE event_id=e.id
                   AND check_type='post_incident' ORDER BY checked_at DESC,id DESC LIMIT 1) k ON TRUE
WHERE e.severity IN ('high','critical') OR e.escalated_to_curator
""",
    },
}
