# Eval baseline

Full run on 2026-11-23 (before fixes), then the 2 failing cases re-run after the fixes below.

## Full run

- Model: `claude-opus-5-5`
- **Passed 15/17 cases (88%)**, 95% CI 66%-97%
- Latency per case: median 14.2s, max 41.5s · wall clock 74s
- Cost: $1.03 total, $0.061 per case

| Case | Tags | Result | Failed checks | Tools | Latency | Cost |
|---|---|---|---|---|---|---|
| compare_miami_houston | assignment, comparison | pass | - | compare_hubs | 17.1s | $0.076 |
| dallas_why_high | assignment, explanation | pass | - | get_hub_risk | 16.3s | $0.073 |
| denver_snow_last_year | assignment, statistic | pass | - | weather_stat | 13.0s | $0.045 |
| followup_compare_it | follow_up, comparison | pass | - | compare_hubs | 41.5s | $0.141 |
| followup_top_one | follow_up, explanation | pass | - | get_hub_risk | 35.3s | $0.106 |
| houston_heavy_rain_window | statistic | pass | - | weather_stat | 11.8s | $0.044 |
| methodology_weights | methodology | pass | - | get_methodology | 16.3s | $0.057 |
| miami_live_alerts | live | **FAIL** | grounded: 'Number of active NOAA alerts at Miami hub' = 1.0 was not found in any tool result | active_alerts | 8.5s | $0.041 |
| midwest_winter_ranking | assignment, ranking | pass | - | rank_hubs | 14.2s | $0.058 |
| minneapolis_hard_freeze_2024 | statistic | pass | - | weather_stat | 11.8s | $0.043 |
| no_dollar_estimates | negative, honesty | pass | - | get_hub_risk | 14.2s | $0.059 |
| northeast_hurricane_top | ranking | pass | - | rank_hubs | 9.2s | $0.040 |
| off_topic | negative, scope | pass | - | - | 5.8s | $0.032 |
| top3_overall | ranking | pass | - | rank_hubs | 9.9s | $0.046 |
| unknown_hub_portland | negative, scope | pass | - | compare_hubs | 16.5s | $0.058 |
| worst_heat_hub | ranking | pass | - | rank_hubs, weather_stat | 14.6s | $0.059 |
| year_outside_data | negative, honesty | **FAIL** | no_figures_with_unit: figures with forbidden unit: ['Denver days with snowfall ≥1 cm, 2025', 'Denver days with any snowfall, 2025'] | weather_stat, weather_stat | 15.8s | $0.051 |

| Check | Pass rate |
|---|---|
| answer_type_in | 9/9 |
| figure | 6/6 |
| grounded | 16/17 |
| hubs_exclude | 1/1 |
| hubs_include | 4/4 |
| no_figures_with_unit | 1/2 |
| ranking_empty | 1/1 |
| ranking_top | 2/2 |
| text_includes_any | 5/5 |
| tools_any | 13/13 |
| top_hub_named | 3/3 |

## Fixes after the full run

| Case | Problem | Fix | Re-run |
|---|---|---|---|
| miami_live_alerts | The agent counted the alerts list itself ("1 alert"), a number no tool returned. The grounding check caught it. | **Agent fix:** `active_alerts` now returns `alert_count`. | pass |
| year_outside_data | The agent correctly refused 2015 and offered clearly labeled 2025 figures, but the check banned any percentage. | **Test fix:** the check now fails only figures labeled 2015 (`no_figure_label_contains`). | pass |

**Result after fixes: 17/17 cases pass** (15 in the full run + 2 re-run). Cost: $1.03 full run + $0.04 re-run.
A clean full run after the fixes has not been done yet: `uv run python -m evals.run`.
