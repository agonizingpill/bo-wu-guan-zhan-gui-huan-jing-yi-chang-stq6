"""Source records and completion rules for sensor-recovery."""

SENSOR_RECOVERY = {
    'sensor-recovery': {
        'title': '采样断档补齐验收',
        'reference_label': '设备工单或补采批号',
        'outcomes': {
            'filled': ('已补有效采样且无超限间隔', ('continuous',)),
            'replace': ('转传感器检修', None),
        },
        'sql': """
SELECT c.code AS source_key,c.code AS subject,
       CONCAT(c.gallery,' · 有效样本 ',COUNT(s.sampled_at),' 条 · 最大间隔 ',COALESCE(ROUND(MAX(s.gap)/60,1),0),' 分钟') AS source_summary,
       CASE WHEN COUNT(s.sampled_at)>=2 AND MAX(s.gap)<=1800 THEN 'continuous' ELSE 'gap' END AS readiness
FROM display_cases c LEFT JOIN (
  SELECT case_id,sampled_at,EXTRACT(EPOCH FROM (sampled_at-LAG(sampled_at) OVER (PARTITION BY case_id ORDER BY sampled_at))) AS gap
  FROM environment_samples WHERE state='accepted'
) s ON s.case_id=c.id WHERE c.status<>'retired' GROUP BY c.id
""",
    },
}
