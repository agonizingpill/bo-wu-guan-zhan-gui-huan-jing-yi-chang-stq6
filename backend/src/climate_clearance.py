"""Source records and completion rules for ambient-clearance."""

CLIMATE_CLEARANCE = {
    'ambient-clearance': {
        'title': '异常后环境解除观察',
        'reference_label': '复测样本凭证号',
        'outcomes': {
            'stable': ('处置后首条复测达标', ('stable',)),
            'monitor': ('移交下班持续观察', None),
        },
        'sql': """
SELECT e.id::text AS source_key,CONCAT(c.code,' · 告警 ',e.id) AS subject,
       CONCAT(e.status,' · 复测 ',COALESCE(s.sampled_at::text,'未复测'),' · 温湿 ',s.temperature,' / ',s.humidity) AS source_summary,
       CASE WHEN s.temperature BETWEEN c.target_temp_min AND c.target_temp_max AND s.humidity BETWEEN c.target_rh_min AND c.target_rh_max
            THEN 'stable' ELSE 'needs_check' END AS readiness
FROM anomaly_events e JOIN display_cases c ON c.id=e.case_id
LEFT JOIN LATERAL (
 SELECT sampled_at,temperature,humidity FROM environment_samples
 WHERE case_id=e.case_id AND sampled_at>e.resolved_at AND state='accepted' ORDER BY sampled_at LIMIT 1
) s ON TRUE
""",
    },
}
