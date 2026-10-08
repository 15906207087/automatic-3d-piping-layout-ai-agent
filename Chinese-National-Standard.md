# National Standards Referenced by CAD-MCP

This file lists the Chinese national standards used by CAD-MCP
(`CAD-MCP/src/cad_controller.py` and the related MCP tools).

Official metadata pages come from the National Public Service Platform for
Standards Information:

https://std.samr.gov.cn/

Notes:

- This repository does **not** include standard full-text PDFs or saved HTML pages.
- Most GB/T recommended standards are not free to redistribute. Obtain official
  text from the national standards disclosure system or other authorized channels.
- Status values below follow the public platform query result at the time of writing
  (all listed items were **current / 现行**).

## Current standards (16)

| Standard No. | Title | Status | Issued | Effective | Use in CAD-MCP | Official page |
| --- | --- | --- | --- | --- | --- | --- |
| GB/T 12459-2025 | 钢制对焊管件 类型与参数 | 现行 | 2025-10-31 | 2026-05-01 | Elbow, tee, reducer OD / wall / C-M sizes | https://std.samr.gov.cn/gb/search/gbDetailed?id=42BA7D06A375E936E06397BE0A0ACDC9 |
| GB/T 28708-2012 | 管道工程用无缝及焊接钢管尺寸选用规定 | 现行 | 2012-09-03 | 2013-03-01 | Steel-pipe OD series | https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D80200D3A7E05397BE0A0AB82A |
| GB/T 9124.1-2019 | 钢制管法兰 第1部分：PN 系列 | 现行 | 2019-05-10 | 2019-12-01 | PN flanges; `draw_flange` / `draw_blind_flange` / `draw_manhole` | https://std.samr.gov.cn/gb/search/gbDetailed?id=88F4E6DA63364198E05397BE0A0ADE2D |
| GB/T 9124.2-2019 | 钢制管法兰 第2部分：Class 系列 | 现行 | 2019-05-10 | 2019-12-01 | Class flanges | https://std.samr.gov.cn/gb/search/gbDetailed?id=88F4E6DA63284198E05397BE0A0ADE2D |
| GB/T 5782-2025 | 紧固件 六角头螺栓 | 现行 | 2025-10-31 | 2026-02-01 | `draw_bolt` | https://std.samr.gov.cn/gb/search/gbDetailed?id=42BA7D06A32DE936E06397BE0A0ACDC9 |
| GB/T 5783-2025 | 紧固件 六角头螺栓 全螺纹 | 现行 | 2025-10-31 | 2026-02-01 | `draw_bolt` (full thread) | https://std.samr.gov.cn/gb/search/gbDetailed?id=42BA7D06A331E936E06397BE0A0ACDC9 |
| GB/T 6170-2015 | 1型六角螺母 | 现行 | 2015-12-31 | 2016-04-01 | `draw_hex_nut` (also written GB/T 6170.1 in code) | https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D80EC8D3A7E05397BE0A0AB82A |
| GB/T 97.1-2002 | 平垫圈 A级 | 现行 | 2002-12-05 | 2003-06-01 | `draw_washer` | https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D79124D3A7E05397BE0A0AB82A |
| GB/T 12221-2025 | 金属阀门 结构长度 | 现行 | 2025-10-31 | 2026-05-01 | Face-to-face length for ball / butterfly / globe / check valves | https://std.samr.gov.cn/gb/search/gbDetailed?id=42BA7D06A2C3E936E06397BE0A0ACDC9 |
| GB/T 12237-2021 | 石油、石化及相关工业用的钢制球阀 | 现行 | 2021-03-09 | 2021-10-01 | `draw_ball_valve` | https://std.samr.gov.cn/gb/search/gbDetailed?id=BD89DE8E07993D08E05397BE0A0A4FAD |
| GB/T 12238-2008 | 法兰和对夹连接弹性密封蝶阀 | 现行 | 2008-12-23 | 2009-07-01 | `draw_butterfly_valve` | https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D7F4C9D3A7E05397BE0A0AB82A |
| GB/T 12235-2025 | 石油、石化及相关工业用钢制截止阀和升降式止回阀 | 现行 | 2025-10-31 | 2026-05-01 | `draw_globe_valve` | https://std.samr.gov.cn/gb/search/gbDetailed?id=42BA7D06A2A5E936E06397BE0A0ACDC9 |
| GB/T 12236-2025 | 石油、化工及相关工业用钢制旋启式止回阀 | 现行 | 2025-10-31 | 2026-05-01 | `draw_check_valve` | https://std.samr.gov.cn/gb/search/gbDetailed?id=42BA7D06A374E936E06397BE0A0ACDC9 |
| GB/T 1226-2017 | 一般压力表 | 现行 | 2017-12-29 | 2018-07-01 | `draw_pressure_gauge` (Y-60 / Y-100 / Y-150) | https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D8299CD3A7E05397BE0A0AB82A |
| GB/T 12465-2017 | 管路补偿接头 | 现行 | 2017-02-28 | 2017-06-01 | `draw_double_flange_limit_expansion_joint` | https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D81549D3A7E05397BE0A0AB82A |
| GB/T 25198-2023 | 压力容器封头 | 现行 | 2023-08-06 | 2024-03-01 | `draw_elliptical_head` | https://std.samr.gov.cn/gb/search/gbDetailed?id=027A6096AEE8643EE06397BE0A0A0867 |

## Version updates from older citations

| Previous citation | Current citation | Notes |
| --- | --- | --- |
| GB/T 12459-2017 | GB/T 12459-2025 | 2025 edition is current |
| GB/T 12221-2005 | GB/T 12221-2025 | 2025 edition replaces 2005 |
| GB/T 12235-2007 | GB/T 12235-2025 | 2025 edition replaces 2007 |
| GB/T 12236-2008 | GB/T 12236-2025 | 2025 edition replaces 2008 |
| GB/T 25198-2010 | GB/T 25198-2023 | 2023 edition replaces 2010 |
| GB/T 5782-2016 | GB/T 5782-2025 | 2025 edition replaces 2016 |
| GB/T 5783-2016 | GB/T 5783-2025 | 2025 edition replaces 2016 |
| GB/T 9119-2010 / GB/T 9123-2010 | GB/T 9124.1-2019 / GB/T 9124.2-2019 | Flange series now cited as the 2019 parts |

## Code mapping

- **Fittings**: `ELBOW_DN_OD_GB12459`, `TEE_CENTER_TO_END_GB12459`, `FITTING_WALL_THICKNESS_GB12459_STD` → GB/T 12459, GB/T 28708
- **Flanges / blinds / manholes**: historical `FLANGE_GB9119` names should be read as GB/T 9124.1 / GB/T 9124.2
- **Fasteners**: `FASTENER_BOLT_GB5782`, `FASTENER_NUT_GB6170_1`, `FASTENER_WASHER_GB97_1`
- **Valves**: `BALL_VALVE_*`, `BUTTERFLY_VALVE_*`, `GLOBE_VALVE_*`, `CHECK_VALVE_*` plus GB/T 12221 lengths
- **Instruments / accessories**: `PRESSURE_GAUGE_GB1226`, `DOUBLE_FLANGE_LIMIT_EXPANSION_JOINT_GBT12465`, `ELLIPTICAL_HEAD_*`

## Numbering notes

- Code `GB/T 6170.1` means current **GB/T 6170-2015** (Type 1 hex nuts), not a separate `6170.1-2016` part.
- The double-flange limit expansion joint also references common product series VSSJA-2(B2F) / JSSJA-2; some non-public envelope sizes use verified engineering tables.

## How to maintain this list

When `cad_controller.py` cites a new or replacement standard:

1. Look up the number at https://std.samr.gov.cn/
2. Update the table (status, dates, official page)
3. Prefer the current edition; keep old numbers only in the version-update table
