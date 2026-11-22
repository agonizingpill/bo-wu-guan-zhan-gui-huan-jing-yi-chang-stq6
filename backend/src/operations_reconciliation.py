"""Additional operational reconciliation rules for this project."""

RECONCILIATION_VIEWS = {
    'sensor-evidence-gaps': (
        '传感采样断档定位',
        '按展柜的有效采样时序定位超过现行业务上限 30 分钟的间隔，辅助补采与校验上传。',
        ['展柜', '采样来源', '前次采样', '当前采样', '实际间隔 分', '核查建议'],
        """
WITH ordered AS (
    SELECT case_id,source,sampled_at,LAG(sampled_at) OVER (PARTITION BY case_id ORDER BY sampled_at) AS previous_at
    FROM environment_samples WHERE state='accepted'
)
SELECT c.code,o.source,o.previous_at,o.sampled_at,
       ROUND(EXTRACT(EPOCH FROM (o.sampled_at-o.previous_at))/60,1) AS gap_minutes,
       CASE WHEN o.sampled_at IS NULL THEN '无有效采样'
            WHEN o.previous_at IS NULL THEN '首个有效采样'
            WHEN o.sampled_at-o.previous_at>INTERVAL '30 minutes' THEN '超过 30 分钟，核对缺测'
            ELSE '采样间隔合规' END
FROM display_cases c LEFT JOIN ordered o ON o.case_id=c.id
WHERE c.status <> 'retired' ORDER BY gap_minutes DESC NULLS LAST,c.code,o.sampled_at DESC
""",
    ),
    'environment-recovery-check': (
        '异常处置后环境复测',
        '用处置关闭后的首条有效采样判断温湿回归情况，并区分尚未处置和待采样。',
        ['展柜', '告警编号', '处置状态', '处置时间', '首条复测', '温度', '湿度', '复测判定'],
        """
SELECT c.code,e.id,e.status,e.resolved_at,s.sampled_at,s.temperature,s.humidity,
       CASE WHEN e.resolved_at IS NULL THEN '待完成处置'
            WHEN s.sampled_at IS NULL THEN '待环境复测'
            WHEN s.temperature NOT BETWEEN c.target_temp_min AND c.target_temp_max
              OR s.humidity NOT BETWEEN c.target_rh_min AND c.target_rh_max THEN '复测仍越限'
            ELSE '复测已回归目标' END
FROM anomaly_events e JOIN display_cases c ON c.id=e.case_id
LEFT JOIN LATERAL (SELECT o.sampled_at,o.temperature,o.humidity FROM environment_samples o
                   WHERE o.case_id=e.case_id AND o.state='accepted' AND o.sampled_at>e.resolved_at
                   ORDER BY o.sampled_at LIMIT 1) s ON TRUE
ORDER BY e.created_at DESC,e.id
""",
    ),
}
