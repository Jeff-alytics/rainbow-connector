#!/usr/bin/env python3
"""Build the measured-sun 20-candidate local review gallery."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "validation" / "arm-lamont" / "review" / "qcrad-top-20.html"

CANDIDATES = [
    ("01", "lamont-155", "2023-06-18 14:20", 73.291, 826.24),
    ("02", "lamont-071", "2024-05-22 13:40", 68.174, 576.99),
    ("03", "lamont-104", "2024-07-27 14:30", 65.877, 700.04),
    ("04", "lamont-125", "2024-06-21 14:05", 63.660, 713.65),
    ("05", "lamont-129", "2023-05-24 23:00", 62.870, 486.18),
    ("06", "lamont-157", "2024-06-09 14:00", 61.419, 496.28),
    ("07", "lamont-021", "2023-10-24 22:05", 58.933, 537.74),
    ("08", "lamont-108", "2025-08-04 22:40", 58.711, 656.29),
    ("09", "lamont-106", "2025-07-16 22:40", 56.592, 637.63),
    ("10", "lamont-153", "2024-09-14 13:25", 55.776, 411.64),
    ("11", "lamont-130", "2025-08-17 22:20", 52.027, 664.78),
    ("13", "lamont-105", "2025-07-13 23:30", 49.697, 644.83),
    ("14", "lamont-139", "2023-05-24 23:50", 47.250, 199.96),
    ("15", "lamont-113", "2023-06-23 23:10", 45.811, 787.16),
    ("16", "lamont-070", "2025-07-08 12:50", 45.725, 334.00),
    ("17", "lamont-003", "2023-06-01 12:55", 43.925, 355.75),
    ("18", "lamont-100", "2024-07-21 00:50", 43.125, 194.34),
    ("19", "lamont-131", "2024-05-15 22:20", 39.678, 545.17),
    ("20", "lamont-140", "2023-09-12 15:05", 38.690, 535.98),
    ("12", "lamont-015", "2023-07-05 23:05", 35.648, 334.72),
]

cards = "\n".join(
    f'<section class="card" data-id="{number}"><h2>{number} · {pair} · {time} UTC · score {score:.3f} · measured DNI {dni:.0f} W/m²</h2><img src="qcrad-{number}-360-screen.jpg" alt="Candidate {number}"><fieldset></fieldset></section>'
    for number, pair, time, score, dni in CANDIDATES
)

html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Measured-sun rainbow candidates — top 20</title><style>
:root{{color-scheme:dark;font:16px/1.45 system-ui,sans-serif}}body{{max-width:1500px;margin:auto;padding:24px;background:#111827;color:#f3f4f6}}header{{position:sticky;top:0;z-index:2;padding:14px 18px;background:#111827ee;border-bottom:1px solid #4b5563}}h1{{margin:0 0 6px;font-size:24px}}p{{margin:5px 0;color:#d1d5db}}.card{{margin:28px 0;padding:18px;background:#1f2937;border:1px solid #4b5563;border-radius:12px}}h2{{margin:0 0 10px;font-size:19px}}img{{display:block;width:100%;height:auto;background:#000;border-radius:6px}}fieldset{{margin-top:14px;border:0;padding:0;display:flex;gap:16px;flex-wrap:wrap}}label{{cursor:pointer;padding:8px 12px;background:#374151;border-radius:6px}}textarea{{width:100%;min-height:180px;background:#030712;color:#fff}}button{{margin-top:8px;padding:9px 14px}}
</style></head><body><header><h1>Measured-sun rainbow candidates — top 20</h1><p>Ranked using one-minute observed direct sunlight, anti-solar radar rain, valid solar geometry, and prior-review exclusion.</p><p>Each candidate contains five full-360° horizon views. Choose “Maybe” when full-resolution retrieval is warranted.</p></header><main>{cards}</main><section class="card"><h2>Review summary</h2><textarea id="summary" readonly></textarea><button id="copy">Copy summary</button></section><script>
const choices=[["no","No bow"],["maybe","Maybe"],["rainbow","Rainbow"],["unclear","Can't tell"]],cards=[...document.querySelectorAll('.card[data-id]')];const update=()=>summary.value=cards.map(c=>`${{c.dataset.id}}: ${{c.querySelector('input:checked')?.value||'unreviewed'}}`).join('\\n');cards.forEach(c=>{{const f=c.querySelector('fieldset');choices.forEach(([v,t])=>{{const l=document.createElement('label');l.innerHTML=`<input type="radio" name="candidate-${{c.dataset.id}}" value="${{v}}"> ${{t}}`;f.append(l)}});f.addEventListener('change',update)}});copy.addEventListener('click',()=>navigator.clipboard.writeText(summary.value));update();
</script></body></html>'''

OUT.write_text(html, encoding="utf-8")
print(OUT)
