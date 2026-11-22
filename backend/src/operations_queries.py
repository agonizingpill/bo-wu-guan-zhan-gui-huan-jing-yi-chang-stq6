"""Business queries over authoritative project records."""

from .operations_reconciliation import RECONCILIATION_VIEWS

VIEWS = {
    'climate-excursions': (
        '温湿越限时间窗',
        '按展柜和采样日将温湿越限、门开背景和有效样本分开计数，便于馆员核对同日环境波动。',
        ['展柜', '采样日', '样本', '温度越限', '湿度越限', '门开采样'],
        """
SELECT c.code,CAST(s.sampled_at AS DATE),COUNT(s.id),
       SUM(CASE WHEN s.temperature NOT BETWEEN c.target_temp_min AND c.target_temp_max THEN 1 ELSE 0 END),
       SUM(CASE WHEN s.humidity NOT BETWEEN c.target_rh_min AND c.target_rh_max THEN 1 ELSE 0 END),
       SUM(CASE WHEN s.door_open THEN 1 ELSE 0 END)
FROM display_cases c JOIN environment_samples s ON s.case_id=c.id GROUP BY c.code,CAST(s.sampled_at AS DATE)
ORDER BY CAST(s.sampled_at AS DATE) DESC,c.code
""",
    ),
    'duty-handover': (
        '值守处置交班',
        '并列展示异常登记时间、升级状态与已有处置次数，供交班时确认异常责任。',
        ['展柜', '异常号', '严重度', '升级', '登记时间', '处置数', '异常状态'],
        """
SELECT c.code,e.id,e.severity,e.escalated_to_curator,
       e.created_at,
       (SELECT COUNT(*) FROM duty_actions a WHERE a.event_id=e.id),e.status
FROM anomaly_events e JOIN display_cases c ON e.case_id=c.id ORDER BY e.created_at DESC,e.id
""",
    ),
    'conservation-evidence': (
        '文物巡检证据链',
        '将每宗异常和对应文物巡检结果一一关联，找出缺少巡检证据的处置记录。',
        ['文物编号', '展柜', '异常号', '门开背景', '巡检项目', '巡检结果', '核验状态'],
        """
SELECT c.artifact_code,c.code,e.id,e.door_context,k.check_type,k.result,
       CASE WHEN k.id IS NULL THEN '缺巡检记录' ELSE '证据已关联' END
FROM anomaly_events e JOIN display_cases c ON e.case_id=c.id LEFT JOIN artifact_checks k ON k.event_id=e.id ORDER BY e.id DESC,k.id
""",
    ),
    **RECONCILIATION_VIEWS,
}
