"""Render an AnalysisResult as text, JSON, or a self-contained HTML page.

Generic over any analysis's output -- this module doesn't know what a
`Verdict` is, or what "waste" means. It only knows the `AnalysisResult`/
`Bucket`/`Slice`/`CoverageStats` shapes from `analysis.py`/`metrics.py`.
Any analysis that produces a conforming `AnalysisResult` gets all three
renderers for free.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from urllib.parse import quote

from .analysis import AnalysisResult, Bucket
from .metrics import CoverageStats, Slice


def to_json(result: AnalysisResult, *, max_reasons: int = 20, indent: int = 2) -> str:
    data = result.as_dict()
    data["reasons"] = {key: reasons[:max_reasons] for key, reasons in result.reasons.items()}
    return json.dumps(data, indent=indent)


def _fmt_usd(value: float) -> str:
    return f"${value:,.4f}" if value < 1 else f"${value:,.2f}"


def _coverage_lines(coverage: CoverageStats) -> list[str]:
    if coverage.total_events == 0:
        return ["Coverage: no events loaded."]

    pct = coverage.pricing_coverage_fraction * 100
    lines = [
        f"Coverage: {coverage.priced_events}/{coverage.total_events} events priced "
        f"({pct:.0f}%) -- {_fmt_usd(coverage.total_priced_cost_usd)} of tracked spend "
        "is what this analysis actually covers."
    ]
    if coverage.unpriced_events:
        lines.append(
            f"  {coverage.unpriced_events} event(s) had no cost_usd and are excluded "
            "from every dollar figure below -- the percentages are computed on the "
            "priced subset, not your total spend."
        )
    conf = coverage.task_id_confidence_fraction
    if conf is not None:
        lines.append(
            f"  Task-id confidence: {coverage.events_confident_task_id}/"
            f"{coverage.events_with_task_id_source_reported} events grouped by a real "
            f"conversation id ({conf * 100:.0f}%); the rest fell back to trace-id "
            "grouping, where cross-trace rework isn't detected."
        )
    for note in coverage.extra_notes:
        lines.append(f"  {note}")
    return lines


def to_text(result: AnalysisResult, *, max_reasons: int = 20) -> str:
    lines: list[str] = []
    lines.extend(_coverage_lines(result.coverage))
    lines.append("")
    lines.append(f"Candidate redundant-repeat pairs: {result.total_candidates}")
    lines.append("")

    for bucket in result.buckets:
        s = bucket.slice
        lines.append(f"{s.count} {bucket.key} -- {bucket.rule_text}")
        lines.append(f"  cost_usd:   {s.cost_usd:.6f}" + (
            f"  ({s.unpriced_count} repeat(s) had no cost_usd)" if s.unpriced_count else ""
        ))
        lines.append(f"  tokens_in:  {s.tokens_in}")
        lines.append(f"  tokens_out: {s.tokens_out}")

        by_model = result.by_bucket_and_model.get(bucket.key, {})
        if by_model:
            lines.append("  by model:")
            for model, ms in sorted(by_model.items(), key=lambda kv: -kv[1].count):
                lines.append(
                    f"    {model}: count={ms.count} cost_usd={ms.cost_usd:.6f} "
                    f"tokens_in={ms.tokens_in} tokens_out={ms.tokens_out}"
                )

        by_workflow = result.by_bucket_and_workflow.get(bucket.key, {})
        if by_workflow:
            lines.append("  by workflow:")
            for wf, ws in sorted(by_workflow.items(), key=lambda kv: -kv[1].count):
                lines.append(
                    f"    {wf}: count={ws.count} cost_usd={ws.cost_usd:.6f} "
                    f"tokens_in={ws.tokens_in} tokens_out={ws.tokens_out}"
                )

        reasons = result.reasons.get(bucket.key, [])[:max_reasons]
        if reasons:
            lines.append("  sample cases (spot-check these by hand):")
            for reason in reasons:
                lines.append(f"    - {reason}")

        lines.append("")

    if result.footnote:
        lines.append(result.footnote)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML
#
# Self-contained: no CDN, no webfonts, no JS framework. This has to run on
# someone's own trace data, possibly offline, and possibly years from now.
# A network dependency is a bug waiting to happen. Colors and chart tokens
# below are lifted from Anthropic's own design system so the page doesn't
# look like a hand-rolled report tool, but every value is inlined; nothing
# is fetched. The CogentWizards org mark embedded below is a small PNG
# (base64, ~6 KB), not a fetch either.
#
# Every string interpolated from the trace (model names, workflow labels,
# classification reasons, all attacker-controlled if the trace comes from
# somewhere untrusted) goes through html.escape(). This file gets opened in
# a real browser; unescaped trace content would be a stored-XSS vector.
#
# Bucket color is assigned BY POSITION in result.buckets, not by key. An
# analysis can have any number of buckets with any keys, so there's no
# fixed enum to hang a color mapping off. Colors are applied via inline
# `style=`, not a `.card-{key}` CSS class: a hardcoded class-per-key
# mapping is exactly the kind of hand-written string that silently drifts
# out of sync with real bucket keys (this file used to have one, matched
# only to the waste analysis's three Verdict values).
#
# "Interactive" here means CSS-only: the theme switch, the model/workflow
# tabs inside each bucket, and the collapsible sections are all built from
# hidden radio/checkbox inputs and sibling selectors, with :has() where a
# plain sibling selector can't reach far enough. No inline event handler,
# no <script> tag, anywhere in this file.
# ---------------------------------------------------------------------------

_PALETTE: tuple[tuple[str, ...], ...] = (
    # (light fill, light stroke, light title, light subtitle,
    #  dark fill, dark stroke, dark title, dark subtitle)
    ("#FAECE7", "#D85A30", "#4A1B0C", "#712B13", "#712B13", "#F0997B", "#F5C4B3", "#F0997B"),
    ("#EAF3DE", "#639922", "#173404", "#27500A", "#27500A", "#97C459", "#C0DD97", "#97C459"),
    ("#F1EFE8", "#5F5E5A", "#2C2C2A", "#444441", "#444441", "#B4B2A9", "#D3D1C7", "#B4B2A9"),
    ("#E8EEF9", "#3B6FD1", "#0F2A5C", "#1F4483", "#1F4483", "#8FB0EA", "#C7D9F5", "#8FB0EA"),
)

_REPO_URL = "https://github.com/CogentWizards/redundo"

# The CogentWizards org mark, downscaled to 64x64 and re-encoded. Embedded
# as data so the header logo needs no network request, matching this
# file's own "nothing is fetched" rule.
_LOGO_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAAXNSR0IArs4c6QAAAERlWElmTU0AKgAAAAgAAYdpAAQAAAAB"
    "AAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAQKADAAQAAAABAAAAQAAAAABGUUKwAAARlElEQVR4Ae1be5hUxZWv"
    "x723u+eBAyIGfK1ZcUEEgrooUSOaBN2IifFb8PMREUWIEI0kghI3MprdyAcohE8QjdmorBAZQeOI8UGi+MAomCgEiB+o0egO"
    "YUaGcZjp7ntvVe059ejuaaaH0Wn4Zynse+tx6tT5/U6970jIoXCIgUMMHGLg/zED9GBjv2z+u30bdn52MiHqaBWL3l7C99Pp"
    "cG8ySDRSEr03fqC3bcqU06KDZddBIaC29kVvfbb3Be0h+Z6S6mxJWH/KOHAAMKk1QQmiRBQxTrdyQp/oE2QefnLOqL8daCIO"
    "OAHnz9x4bpvw74yFOotSjygZAyZpwAMDeQ6sKcwDToAcGX4a+Hzxsaph7rL557cdKCIOGAEX1dZXNLf0vz2SfDplXqDibM7b"
    "FL2uLPgCZEiGDlBGoIcwHhBOojdTnpz2wtxTNrricr4PCAEX1W7s29zClsU0eYGKM4RaT6PhpkGbo91fBKcoj3kJwqhoSlFx"
    "2dr5p6wtku5xsuwEjLn5ucq2uM9qySrHiKhdG4ge76wh7fECwCjj8nS2rqQIZQGQIJt8Fn5r3d0jN/QYdYECVhDveVQp2pqt"
    "WSRoKgfeKUVAGpzLcG/LTDFBmgwcJlBJiZAIxfqGsbfi/J9s7O+qluNdVgLO/vHGSxRLXCN1tzfdPed7i94Rge9C0C7f0ASp"
    "gkIsI0AC4cl/bk+Tu8oB3OkoGwEXTa6viGNSq41Ft2HQIDBuf9qdtszmoliuazhRnbnvQ0ZpEivvitEz3zpj39IvllM2Alqr"
    "+p3HEr1O5twjfiJFuJ8k3EsRnMQILn8dXApINUmWDOttvTgADp1rizrCgiFBPS+K5IyO+V88VdDRvrgSrPm1mzZf4nuVQ0R7"
    "+u/Jyqq29nS7z5n8UkzkYEHCU2HlH0J5IoDNDmAXpjHdem6QWOBmedQCSEKxhcASo6q1JhUNe7YMGyXPWNKz57gHr+0TB7fv"
    "CuO2au4H/WIhj64mVHmUNIOjn6xUR93dvOWGoC2U35FUfV/yxACJ+wII6PWcywFwp47XkvahJKE8Vb03jL4NOYsKi75IvJjf"
    "buvAzvjdh678Jnj4KkXEaEXJURQQ5xBoZJCMJez+1Gecy3UBzdwf/WXW5j3p6p/GjExSCnaFOdAWOqYxai3L8WMtwzSFYeWp"
    "cNmuf1k9S4XV5xyXJGvWTrm1pdvGFwjaZgpyuhH97q8uO0v45I5YivMIgJYAkkjXdS0ia7mFBXMBIwxQcSnXBCp9Y9Om+Rdn"
    "VXa+kgIkQcpZYqs7MwqycyKU+4Sr6LXW4393bZNQWxnj7yUonXfMnvDXL9XW4l6728Hp71aFq399dbJJpe8QVN7EfBaICPf0"
    "BrhT5GjQiHRZASLoFV7AoVeo/00EjRc2vr7oXOmpeyTMCzl0hZY49jAv3wDEGeFUvu8du330DvnuH6XvD8DNViDp76oY/f6W"
    "H976UaGaruLdXgWuWHRFryaarlMBmamUCkQotE3YMLPd3diJk5q1Fl46juVWJs7CBMjIgGx4xFMDTp9bT0X8CmW+7fdFpkI1"
    "JFTzkCMDIzqRJO3cg9VFEAHDLIpJxOm/tQjx3NAFd325SFPJZLcIGLd4XFVLlXiU+HSsQAA2ABHQAaxlBSRgMVLgwJu4yYO5"
    "kYhIEJb0jsmQ9qtYVHE75MAA0mgNNofRqkZ9Osv2NpNWMvAibBWOjiaoKCLS44P2ELV68IL/6taOsTsE0HQFv4cmCICPwZEI"
    "Jx/cPl+D1GU6lhfAGOS7ekgKxmU2jn2Reu7VpYNeZjLeqo/KBQDzClAfwHdEa30wBAhtbq1ogiMjq9ZZmnKIAQnC48Ph/PzL"
    "cStXBljWVdgvAd9+8NJ/V1xeFwF4HcAeBMBgHDrw1reu2Jmi7QaH59PWEubDPCDIm6sm/M96KJWMkacpbKBMKHC7zdHuRx4w"
    "oD68LyB0x86gqQpWnyqK5BTwpLIhiT3vwrca3rtG1+ni0SUB4xZPrRJM3tnBJJtwefjW4xQiekiYlG7Sjl5tdG6oQAn2As78"
    "hUCgVuN78cMqSmcMCi2uMXdqNzqAc5gE2QY4HgxmHixDuudZO2z7CvbloYhnDV2ypHenemxmlwRkkrsvhXE/CJc55wCLBqrD"
    "xINd1v6M2QVNWYY0OQVw0PskJn+sqDzxt076pbu/+lfYNdYx2D5jO6YtU1N3fQ3Q5OohJDLiMC+1hkpvTOHIcDW1XiFgPvCP"
    "3Zttvty109m7JAHjVo7jsYqvkzA9mQAGWKZ12mVDwpiGb/MvjxdKQE57HyylHNM0wxWbUTe+Fo53+VCZUD+jMvMpnhuwktbp"
    "2nAkQxoul8D75NWG4x9vloSPleBpLQ+2oXnWAqNYSgK+mzj6xVo3vvIN2lhJAkg2cQIM9OFKWGO0Ra4+JvJNGTuNgOv25u1K"
    "QJbDnMF45FF+ff21K151mtz7hXlnbE/5ciIjaq85POkmbDHa4HQpUkN6zd4VyQmK88N1D0G6oFhboE2DB7ChgADYep/cuDk4"
    "0bVT/C5JQDqKvsp8mtQe7FALlUMG/oqC8bS2RT9092Sw+Un6MFn7u1hIrqi/avlDRdVyyT/MH1nvs9bvwCZnB54k9QpX0Md5"
    "kCIeiZZ/+OXffBQr9iNc+zua0TGlyfG9REaokblGiiIlCYC1ephGiTrRCP3rWFt7GR2jf/hwATwGW18v4eFq8SmJ1MJKxs9c"
    "M+mxOidR6v3ywnP+0Ley+ayAtM/mVHyM8wKDm2Lup4ivsq8MPCy8oTkiSxRjvZ33sS/mmTA9M79CwZQjxdBS7ZUeG4z1xUoa"
    "PzwMPOyKNqHbzF93YjnK4oN7AFzSLSSmD6dC/7G6KY98hEXdDU/9/Bv/ANk7x816+b6mkFwZUj4Zboc/+adkfOlzvdbOVn5w"
    "gQpx+4wtakOcgTkb3LyjezDjGktn7ZckAHZsFQaShmXqAkrtdcyCBdiVmPEOafA6XN7tyZLgP34/4ZGlYGB+29hZ6/vJq7vr"
    "a40gsgD0Lzp7zgNHrUs1zVU0uEYCeN02jnM9RCClbbJuMN4y2qEc5gIcT52GkkMAJq0QuxEG06l0xDYEcVAsUTkKwANPe0TQ"
    "bXto5ailL/25cevUU294Y9bXD8finoRR99zTZ+DCOVP+lmh8Pqb8Gtzu6uBss2/MM/aiTZYBKMM8mIZKfmor2QOoJJ9hF0MF"
    "OZZ1y6Dc0A8pGBLYFs7wijU1HtZ/7KPPvHBRzMUCX6Vp5e7s9M3TRjwqU9W/HD7/5Q909W4+Bv9i7sC0FNP+rtKXwBeSY6QE"
    "gh141IGeB9uKQ44EtAsDyii62yT2fZYkwPO9DwSgM+D3ragNgGxsB0+DbSw5deXTz/5rxicLBWxCIgm7PSqPTVA5K2oNp2yZ"
    "NuLeOHXE3OHzn+/yMxd4PPUPkpnRKqPpyvNq8EKFxGYkUdh+o3f3hW3sQ07MUDBplEP8Hufvmpx9n6WHgFTrJXzQM4y6io5W"
    "qxlecC8AQ8FbvWr9R+vbPLpUwZWVnSrxjoRk4M5AibhPUmRu520769+5eUw/p634Pejenx/+MWlfnfHYHUKpGhnCXsluxDRo"
    "eBh7dCpfHZFr9O6Ng9YEmCzjwONw5ug8lCSAV9T8ScbqYwoDKB9cHN+GDKqoaO1VM2d33HhzksoadJimHUXsD7PagYiUis5l"
    "rQ3L1k8ft8+kNKS2NmjPyoeE7+kZ3h2inBpQYZrEZjVgnWPNQKlOAg5Not47sql9ayelOqskAXXjl+zljD2O21c9reQYhnrW"
    "AJz4oJO8+JsXt3+SJWJCBi4mjMGuDzgOzFyCJFQwMaY6fP+6YoNaq/wpAH6sysIMn0MNEZ3IS7shWZQNAo4ErGPk4aBEAuYv"
    "h2syOGh1HkoSgOKBYvfJUObHrHG61oRR3N7CpczTLN3w9SQTvTVHuhRJQzvQEmcY5EA0i+M5zs7YdOuFuVPasHnz+sVE3IYn"
    "OFPD1nQo84pBh9Pn3rpB0+mwzGXDGZuE0e7qRPArI9H5s0sCVk1cvh0uax7ygvxc6WzBduBuj2QrKje0UXWWbtkal1+GihqF"
    "vQN0EpKg4miyt+FiV9rKwwk0CI7E8a7HuAYBD+DMeTw3xm0lnY8sdwh27CPXPuwelVr8zvU/+qSDSFGiSwJQNhGLO0Uod+Am"
    "x7SHKwMUAFgwIt1f+btiSk7CcY7s45xhzAAhlNPCNm41YB6NwquwygmLFiVCKeB7IvQMBO7AY1Unr/N0BhQbkpAo84MqrtzK"
    "U3CYF8VvHB0H86BWl2G/BDxx/RO7vJhPgsmujekJEVrDBuEH9/3Np+xswT8AOBJv9XJBi2iCDAgswjysByHCE6aSI9W0wcft"
    "kenz4VQ3iEggQBMGAkiaJk6LmwdWdgosUFMBi61irIOXJVI2BEROfO2WW1oLNHQa3S8BWKt+8op1sl1MhINNmkNP0D4GY8AD"
    "6RNQgPFKfBkAiAKCtldLGvsKMGFvSTJVsSNiM1ppMgRFMt/1TX2rRava92EB5wqstA+nTkIaq6h36fbpP92WK+4i0i0CsP6z"
    "Ux+vY1l2OXze2IV3+zooEuAWiymZdQ7SjsPeYP4r8JqpYuuRDKyXgqlJH/516Z524f+YeT4MHzTHTKAoZ3QCWMRne4SFisU2"
    "2wwUGuDHErW1F2Vjt9448xUt0I1HtwlAXfVTVjzpR/5ouNJ6HpZIOKbSfm8f3iuAuaAFjUXjCn2Tj+djziZDlEzsIXx5845H"
    "nkrTxFTQGSIQ1JUPBZCxkk7iw84F4HUgTvhCPNxLyfO23HjLm/m6+499LgJQ3RPXLduWekZ8ixP/QhgSz7yf4AFsWnbwHANg"
    "feHmyYEpQKUnL6BKzwUiPv5dpdZt3fbI9iZWdSZcNT4Nn7oETybgdIngoLdhz7A/6sGFOKzvVHd32hYQWlfFvXM++OFPrv7L"
    "TbfhMfpzBWfe56rUQRgcsXnaV2ZXyOzsdlgW9VhGN2lP5SVzSYjkuACPYr4Pmy1JWZTg/gIvccT9p550ZW+e3XOxL8NRVMqB"
    "oKwa6jAQD2HibWScbw0o2diLB0/9adr0bo31vCUdYz0nAPS9c8OZZ/iZ3a/Crt9ODqBW93HbWA4xpAvzbTGu6dgVE3BjnJVs"
    "N3zofLwPjeozlb13/PeAUW3Py/7eLs9TQ1mLnNT6oRrTtCmmC99qsNV79CoLAStXruQn/f4/XwtodDp8MrQBfOvc7gjQWa6X"
    "ODHMzK34evQEsMPElQJ2zjC50p2w3W6BjBAOgxUVCa9v2ksuHLpoQ1n+VuhzzwEOXuF7/PjxQvmJe/G4mg8F3KLXNUgAjwKO"
    "GIwjOfqHL73NIXCJSUI8VUmRoEocl2BqWMJXpwVMnpTORkdkQlWPVcsRCi3ukb5W31uVVuytADTm/dlRpQZvweZKct43hDlu"
    "HBm4dY5gWQ0hghOtZP6qNV8aW/J0l9PbzYhptZvC+xPbNG3kKD/auxbuBCrAifsExA5+zufnwOezOsSw59iAN26S8iZSedio"
    "IQte3+Hye/ouWw9AQ4YtfvP1DAlm4pY5p9jiRSjwd5Q5e3Hic/D0ymHYMeUFwDEDV1X4eBpnaeLGcoLXuk2L5XuOeODtxVnF"
    "b4O/9IarQrDcokToBqODbca8A5+jxhVbEvBrGlxpqQxLzDzl/j+vKJ+lRlOu3XIr3vSD06YHcWYOTGIBjmFHhMOH7eFJoXBE"
    "uIlSywIBCej3gvK2kPk3Db/v7QfLbaOx4UBotTrfnnr6uQHJzgtUeKowHyoRo+NCS+XnBVegiA89Bz0TE/j/LLzUjBFLNpS8"
    "0+up+QesBzjDNtZOrqjYvelyGqenwh9JjkjCVknADAmXnvq+Exc+vFXG4YIjJgO3yTDqN5JEcpHqc/xjQ2rrOnxFdnrL9T7g"
    "BDhDt8ClJ2leO5LItm/CtdBQKeRxjLEadDV8uWmB+IdwmfIOS9SsbT3xxDdOm/JAyY8ZTmc53geNgGJja2sVm9z/jiTmP9Aw"
    "O1NbC59iDoVDDBxi4BADhxg4uAz8H8kpPmRc9EyZAAAAAElFTkSuQmCC"
)


def _redundo_version() -> str:
    try:
        return _pkg_version("redundo")
    except PackageNotFoundError:
        return "dev"


def _mailto_share_href() -> str:
    subject = "redundo waste report"
    body = (
        "Sharing a redundo waste-detection report (attached).\n\n"
        f"redundo is open source: {_REPO_URL}"
    )
    return f"mailto:?subject={quote(subject)}&body={quote(body)}"


def _palette_for(index: int) -> tuple[str, ...]:
    return _PALETTE[index % len(_PALETTE)]


def _bar_chart_svg(buckets: list[Bucket]) -> str:
    """One horizontal bar per bucket. Uses cost_usd if any bucket has priced
    repeats, otherwise falls back to call count -- an all-zero dollar chart
    would just be misleading. Each bar is a link to that bucket's section.
    """
    use_cost = any(b.slice.cost_usd > 0 for b in buckets)
    values = [b.slice.cost_usd if use_cost else b.slice.count for b in buckets]
    max_value = max(values) or 1

    width, row_h, gap, label_w, chart_w = 640, 40, 14, 150, 400
    height = len(buckets) * (row_h + gap) - gap + 8
    bars: list[str] = []

    for i, bucket in enumerate(buckets):
        light_fill, light_stroke, *_ = _palette_for(i)
        y = i * (row_h + gap) + 4
        bar_w = (values[i] / max_value) * chart_w if max_value else 0
        value_text = _fmt_usd(bucket.slice.cost_usd) if use_cost else f"{bucket.slice.count} pair(s)"
        anchor = html.escape(f"#bucket-{bucket.key}", quote=True)
        bars.append(
            f'<a href="{anchor}" class="bar-link">'
            f'<text x="0" y="{y + row_h / 2 + 5}" class="bar-label">{html.escape(bucket.label)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{chart_w}" height="{row_h}" class="bar-track" rx="4"/>'
            f'<rect x="{label_w}" y="{y}" width="{max(bar_w, 2)}" height="{row_h}" '
            f'fill="{light_fill}" stroke="{light_stroke}" stroke-width="1.5" rx="4"/>'
            f'<text x="{label_w + chart_w + 12}" y="{y + row_h / 2 + 5}" class="bar-value">{html.escape(value_text)}</text>'
            f'</a>'
        )

    axis_label = "Cost (USD)" if use_cost else "Candidate pairs (no cost_usd on any repeat)"
    return (
        f'<svg viewBox="0 0 {width} {height + 24}" width="100%" role="img" '
        f'aria-label="Bar chart comparing {axis_label.lower()} across the classification buckets, '
        f'click a bar to jump to that bucket">'
        f'<title>{html.escape(axis_label)} by bucket</title>'
        f'{"".join(bars)}'
        f'<text x="{label_w}" y="{height + 20}" class="axis-label">{html.escape(axis_label)}</text>'
        f'</svg>'
    )


def _coverage_html(coverage: CoverageStats) -> str:
    if coverage.total_events == 0:
        return "<p>Coverage: no events loaded.</p>"

    pct = coverage.pricing_coverage_fraction * 100
    parts = [
        f"<p>Coverage: <strong>{coverage.priced_events}/{coverage.total_events} events "
        f"priced ({pct:.0f}%)</strong> -- <strong>{html.escape(_fmt_usd(coverage.total_priced_cost_usd))}"
        "</strong> of tracked spend is what this analysis actually covers.</p>"
    ]
    if coverage.unpriced_events:
        parts.append(
            f"<p>{coverage.unpriced_events} event(s) had no cost_usd and are excluded "
            "from every dollar figure below -- the percentages are computed on the "
            "priced subset, not your total spend.</p>"
        )
    conf = coverage.task_id_confidence_fraction
    if conf is not None:
        parts.append(
            f"<p>Task-id confidence: {coverage.events_confident_task_id}/"
            f"{coverage.events_with_task_id_source_reported} events grouped by a real "
            f"conversation id (<strong>{conf * 100:.0f}%</strong>); the rest fell back to "
            "trace-id grouping, where cross-trace rework isn't detected.</p>"
        )
    for note in coverage.extra_notes:
        parts.append(f"<p>{html.escape(note)}</p>")
    return "".join(parts)


def _metric_card(bucket: Bucket, index: int) -> str:
    _, stroke, *_ = _palette_for(index)
    s = bucket.slice
    label = html.escape(bucket.label)
    headline = _fmt_usd(s.cost_usd) if s.cost_usd > 0 else f"{s.count} pair(s)"
    sub_parts = [f"{s.count} pair(s)"]
    if s.tokens_in or s.tokens_out:
        sub_parts.append(f"{s.tokens_in:,} in / {s.tokens_out:,} out tokens")
    if s.unpriced_count:
        sub_parts.append(f"{s.unpriced_count} unpriced")
    # Escape each part individually, then join with a literal (safe) middle
    # dot -- html.escape()-ing the already-joined string double-escapes the
    # entity itself ("&middot;" -> "&amp;middot;"), which renders as the
    # literal text "&middot;" instead of a middle dot.
    subtitle = " &middot; ".join(html.escape(p) for p in sub_parts)
    anchor = html.escape(f"#bucket-{bucket.key}", quote=True)
    return (
        f'<a class="card" href="{anchor}" style="border-left-color: {stroke};">'
        f'<p class="card-label">{label}</p>'
        f'<p class="card-headline">{html.escape(headline)}</p>'
        f'<p class="card-sub">{subtitle}</p>'
        f'</a>'
    )


def _breakdown_table(title: str, rows: dict[str, Slice]) -> str:
    if not rows:
        return ""
    body = "".join(
        f'<tr><td>{html.escape(key)}</td><td class="num">{s.count}</td>'
        f'<td class="num">{html.escape(_fmt_usd(s.cost_usd))}</td>'
        f'<td class="num">{s.tokens_in:,}</td><td class="num">{s.tokens_out:,}</td></tr>'
        for key, s in sorted(rows.items(), key=lambda kv: -kv[1].count)
    )
    return (
        f'<div class="table-scroll"><table class="breakdown"><caption>{html.escape(title)}</caption>'
        f'<thead><tr><th>{html.escape(title)}</th><th class="num">Count</th>'
        f'<th class="num">Cost</th><th class="num">Tokens in</th><th class="num">Tokens out</th></tr></thead>'
        f'<tbody>{body}</tbody></table></div>'
    )


def _breakdown_tabs(key: str, by_model: dict[str, Slice], by_workflow: dict[str, Slice]) -> str:
    """Renders the model/workflow breakdowns for one bucket. Uses a CSS-only
    tab pattern (hidden radio inputs, sibling selectors keyed on a shared
    data-tab vocabulary) when both are present, so they don't have to sit
    side by side and overflow a narrow viewport. Falls back to a single
    table, no tab UI at all, when only one of the two is present.
    """
    model_table = _breakdown_table("By model", by_model)
    workflow_table = _breakdown_table("By workflow", by_workflow)
    if model_table and workflow_table:
        safe_key = html.escape(key, quote=True)
        return (
            f'<div class="tabset">'
            f'<input type="radio" name="tabs-{safe_key}" id="{safe_key}-tab-model" data-tab="model" checked>'
            f'<input type="radio" name="tabs-{safe_key}" id="{safe_key}-tab-workflow" data-tab="workflow">'
            f'<div class="tab-strip">'
            f'<label for="{safe_key}-tab-model">By model</label>'
            f'<label for="{safe_key}-tab-workflow">By workflow</label>'
            f'</div>'
            f'<div class="tab-panel" data-tab="model">{model_table}</div>'
            f'<div class="tab-panel" data-tab="workflow">{workflow_table}</div>'
            f'</div>'
        )
    return model_table or workflow_table


def _bucket_section(result: AnalysisResult, bucket: Bucket, *, index: int, max_reasons: int) -> str:
    by_model = result.by_bucket_and_model.get(bucket.key, {})
    by_workflow = result.by_bucket_and_workflow.get(bucket.key, {})
    reasons = result.reasons.get(bucket.key, [])[:max_reasons]

    reasons_html = ""
    if reasons:
        items = "".join(f"<li>{html.escape(r)}</li>" for r in reasons)
        reasons_html = (
            '<details class="reasons"><summary>Sample cases '
            f'(spot-check these by hand, {len(reasons)} shown)</summary>'
            f'<ul>{items}</ul></details>'
        )

    tabs_html = _breakdown_tabs(bucket.key, by_model, by_workflow)

    # Only the first bucket opens by default -- usually the most interesting
    # one (confirmed_waste), and it keeps the initial view from being a wall
    # of expanded tables for every bucket at once.
    open_attr = " open" if index == 0 else ""
    safe_id = html.escape(f"bucket-{bucket.key}", quote=True)

    return (
        f'<details class="bucket" id="{safe_id}"{open_attr}>'
        f'<summary><h2>{html.escape(bucket.label)} <span class="count-pill">{bucket.slice.count}</span></h2></summary>'
        f'<div class="bucket-body">'
        f'<p class="rule">{html.escape(bucket.rule_text)}</p>'
        f'{tabs_html}'
        f'{reasons_html}'
        f'</div>'
        f'</details>'
    )


def _header_html() -> str:
    logo_src = f"data:image/png;base64,{_LOGO_BASE64}"
    return f"""
<header class="page-header">
  <div class="brand">
    <img src="{logo_src}" width="28" height="28" alt="" class="brand-mark">
    <span class="brand-name">redundo</span>
  </div>
  <div class="theme-toggle" role="group" aria-label="Theme">
    <input type="radio" name="theme" id="theme-auto" checked>
    <label for="theme-auto" title="Match system theme">Auto</label>
    <input type="radio" name="theme" id="theme-light">
    <label for="theme-light" title="Light theme">Light</label>
    <input type="radio" name="theme" id="theme-dark">
    <label for="theme-dark" title="Dark theme">Dark</label>
  </div>
</header>
"""


def _footer_html() -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    version = html.escape(_redundo_version())
    mailto = _mailto_share_href()
    return f"""
<footer class="page-footer">
  <p class="tagline">Generated with <strong>redundo</strong>. Point it at your agent's OTLP traces, get a report on what's wasted.</p>
  <p class="reassurance">This file is self-contained. No data left your machine to produce it.</p>
  <p class="links">
    <a href="{_REPO_URL}">GitHub</a>
    <a href="{_REPO_URL}/tree/main/docs">Docs</a>
    <a href="{_REPO_URL}/blob/main/LICENSE">License</a>
    <a href="{_REPO_URL}/issues">Report an issue</a>
  </p>
  <p class="share">
    <a href="{mailto}">Share this report by email</a>
    &middot; attach this file, it's the whole report, nothing else needed.
    Printing to PDF (your browser's own Print dialog) works too.
  </p>
  <p class="meta">Generated {generated} &middot; redundo v{version}</p>
</footer>
"""


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analysis report</title>
<style>
:root {{
  --page-bg: #ffffff; --card-bg: #fcfcfb; --text-primary: #0b0b0b;
  --text-secondary: #52514e; --text-muted: #898781; --border: rgba(11,11,11,0.10);
  --gridline: #e1e0d9; --accent-bg: #e1e0d9;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --page-bg: #151514; --card-bg: #1a1a19; --text-primary: #f0efec;
    --text-secondary: #c3c2b7; --text-muted: #898781; --border: rgba(255,255,255,0.10);
    --gridline: #2c2c2a; --accent-bg: #2c2c2a;
  }}
}}
/* Manual override, wins over prefers-color-scheme either direction because
   a body:has() selector is more specific than a bare :root rule. */
body:has(#theme-light:checked) {{
  --page-bg: #ffffff; --card-bg: #fcfcfb; --text-primary: #0b0b0b;
  --text-secondary: #52514e; --text-muted: #898781; --border: rgba(11,11,11,0.10);
  --gridline: #e1e0d9; --accent-bg: #e1e0d9;
}}
body:has(#theme-dark:checked) {{
  --page-bg: #151514; --card-bg: #1a1a19; --text-primary: #f0efec;
  --text-secondary: #c3c2b7; --text-muted: #898781; --border: rgba(255,255,255,0.10);
  --gridline: #2c2c2a; --accent-bg: #2c2c2a;
}}
* {{ box-sizing: border-box; }}
body {{
  background: var(--page-bg); color: var(--text-primary);
  font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 16px; line-height: 1.6; margin: 0;
}}
a {{ color: inherit; }}
.page-header, .page-footer, main {{ max-width: 760px; margin: 0 auto; padding-left: 1.5rem; padding-right: 1.5rem; }}
.page-header {{
  display: flex; align-items: center; justify-content: space-between;
  padding-top: 1.25rem; padding-bottom: 1.25rem; border-bottom: 1px solid var(--border);
  margin-bottom: 2rem;
}}
.brand {{ display: flex; align-items: center; gap: 8px; }}
.brand-mark {{ border-radius: 6px; display: block; }}
.brand-name {{ font-size: 15px; font-weight: 600; letter-spacing: 0.01em; }}
.theme-toggle {{ display: flex; gap: 2px; background: var(--card-bg); border-radius: 999px; padding: 2px; }}
.theme-toggle input {{ position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }}
.theme-toggle label {{
  cursor: pointer; font-size: 12px; color: var(--text-muted); padding: 4px 10px;
  border-radius: 999px; user-select: none;
}}
.theme-toggle input#theme-auto:checked ~ label[for="theme-auto"],
.theme-toggle input#theme-light:checked ~ label[for="theme-light"],
.theme-toggle input#theme-dark:checked ~ label[for="theme-dark"] {{
  background: var(--accent-bg); color: var(--text-primary);
}}
main {{ padding-top: 0; padding-bottom: 3rem; }}
h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 4px; }}
h2 {{ font-size: 18px; font-weight: 500; margin: 0; display: flex; align-items: center; gap: 8px; }}
.subtitle {{ color: var(--text-secondary); font-size: 14px; margin: 0 0 2rem; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 2rem; }}
.card {{
  background: var(--card-bg); border-radius: 12px; padding: 1rem 1.1rem; border-left: 3px solid;
  display: block; text-decoration: none; transition: transform 0.1s ease;
}}
.card:hover {{ transform: translateY(-1px); }}
.card-label {{ font-size: 13px; color: var(--text-secondary); margin: 0 0 6px; }}
.card-headline {{ font-size: 22px; font-weight: 500; margin: 0 0 4px; }}
.card-sub {{ font-size: 12px; color: var(--text-muted); margin: 0; }}
.chart {{ margin: 0 0 2.5rem; }}
.bar-link {{ cursor: pointer; }}
.bar-link .bar-track {{ transition: opacity 0.1s ease; }}
.bar-link:hover .bar-track {{ opacity: 0.3; }}
.bar-label {{ font-size: 13px; fill: var(--text-secondary); }}
.bar-value {{ font-size: 13px; fill: var(--text-primary); }}
.axis-label {{ font-size: 12px; fill: var(--text-muted); }}
.bar-track {{ fill: var(--gridline); opacity: 0.5; }}
details.bucket {{ padding: 1.25rem 0; border-top: 1px solid var(--border); }}
details.bucket summary {{ cursor: pointer; list-style: none; }}
details.bucket summary::-webkit-details-marker {{ display: none; }}
details.bucket summary h2::before {{
  content: "\\25B8"; display: inline-block; font-size: 13px; color: var(--text-muted);
  transition: transform 0.1s ease; width: 1em;
}}
details.bucket[open] summary h2::before {{ transform: rotate(90deg); }}
.bucket-body {{ padding-top: 1rem; }}
.count-pill {{
  font-size: 13px; font-weight: 500; padding: 2px 10px; border-radius: 999px;
  background: var(--gridline); color: var(--text-secondary);
}}
.rule {{ font-size: 13px; color: var(--text-secondary); margin: 0 0 1rem; }}
.coverage {{
  background: var(--card-bg); border-radius: 12px; padding: 0.9rem 1.1rem;
  font-size: 13px; color: var(--text-secondary); margin: 0 0 1.5rem;
}}
.coverage p {{ margin: 0 0 4px; }}
.coverage p:last-child {{ margin-bottom: 0; }}
.coverage strong {{ color: var(--text-primary); font-weight: 500; }}
.tabset {{ position: relative; }}
.tabset input {{ position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }}
.tab-strip {{ display: flex; gap: 4px; margin-bottom: 10px; }}
.tab-strip label {{
  cursor: pointer; font-size: 13px; color: var(--text-secondary); padding: 4px 12px;
  border-radius: 999px; user-select: none;
}}
.tabset input[data-tab="model"]:checked ~ .tab-strip label[for$="-tab-model"],
.tabset input[data-tab="workflow"]:checked ~ .tab-strip label[for$="-tab-workflow"] {{
  background: var(--gridline); color: var(--text-primary);
}}
.tab-panel {{ display: none; }}
.tabset input[data-tab="model"]:checked ~ .tab-panel[data-tab="model"],
.tabset input[data-tab="workflow"]:checked ~ .tab-panel[data-tab="workflow"] {{ display: block; }}
.table-scroll {{ overflow-x: auto; margin-bottom: 1rem; }}
table.breakdown {{ border-collapse: collapse; font-size: 13px; width: 100%; min-width: 420px; }}
table.breakdown caption {{ display: none; }}
table.breakdown th, table.breakdown td {{ padding: 6px 10px 6px 0; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }}
table.breakdown th {{ color: var(--text-muted); font-weight: 500; }}
table.breakdown td.num, table.breakdown th.num {{ text-align: right; padding-right: 4px; }}
details.reasons {{ font-size: 13px; color: var(--text-secondary); }}
details.reasons summary {{ cursor: pointer; color: var(--text-primary); margin-bottom: 8px; }}
details.reasons ul {{ margin: 0; padding-left: 1.2rem; }}
details.reasons li {{ margin-bottom: 4px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; }}
.page-footer {{
  margin-top: 1rem; padding-top: 1.5rem; padding-bottom: 2.5rem; border-top: 1px solid var(--border);
  font-size: 13px; color: var(--text-secondary);
}}
.page-footer p {{ margin: 0 0 8px; }}
.page-footer .tagline {{ color: var(--text-primary); }}
.page-footer .links a {{ margin-right: 14px; text-decoration: underline; }}
.page-footer .meta {{ color: var(--text-muted); font-size: 12px; margin-bottom: 0; }}
@media print {{
  .page-header .theme-toggle, .page-footer .links, .page-footer .share {{ display: none; }}
  details.bucket, details.reasons {{ break-inside: avoid; }}
  details > *:not(summary) {{ display: block !important; }}
  summary::-webkit-details-marker, summary::marker {{ display: none; }}
  a {{ text-decoration: none; }}
}}
</style>
</head>
<body>
{header}
<main>
<h1>Analysis report</h1>
<p class="subtitle">{total_pairs} candidate redundant-repeat pair(s) evaluated</p>
<div class="coverage">{coverage}</div>
<div class="cards">{cards}</div>
<div class="chart">{chart}</div>
{sections}
<p class="footnote">{footnote}</p>
</main>
{footer}
</body>
</html>
"""


def to_html(result: AnalysisResult, *, max_reasons: int = 20) -> str:
    """Render a self-contained HTML page: no CDN assets, no webfonts, no JS.
    Safe to open directly from disk or attach anywhere -- every value pulled
    from the trace is HTML-escaped before being interpolated. The header,
    theme toggle, per-bucket tabs, and collapsible sections are all CSS-only
    interactivity; there is no <script> tag anywhere in the output.
    """
    coverage_html = _coverage_html(result.coverage)
    cards = "".join(_metric_card(b, i) for i, b in enumerate(result.buckets))
    chart = _bar_chart_svg(result.buckets)
    sections = "".join(
        _bucket_section(result, b, index=i, max_reasons=max_reasons)
        for i, b in enumerate(result.buckets)
    )

    return _HTML_TEMPLATE.format(
        header=_header_html(),
        footer=_footer_html(),
        total_pairs=result.total_candidates,
        coverage=coverage_html,
        cards=cards,
        chart=chart,
        sections=sections,
        footnote=html.escape(result.footnote or ""),
    )
