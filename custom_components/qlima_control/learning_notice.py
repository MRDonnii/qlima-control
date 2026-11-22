"""Notifications in Home Assistant that explain the Follow Me learning while it happens.

A check is only announced once it lasts longer than a normal unit needs to react, so
everyday starts and stops stay quiet. The texts say what was seen, why it happens and
what will change – so nobody takes the learning for a fault.
"""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant


def _n(value, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}".replace(".", ",")
    except (TypeError, ValueError):
        return "–"


def _words(c: dict) -> tuple[str, str]:
    """(past the target, short of the target) – 'over'/'under' for heat, mirrored for cool."""
    return ("under", "over") if c.get("cool") else ("over", "under")


def _told(c: dict, level: int) -> int:
    """How far from its target the unit was told the room is – the value sent, else the level."""
    return c["told"] if isinstance(c.get("told"), int) and c["told"] > 0 else level


def _parked(c: dict) -> str:
    return "maksimum" if c.get("cool") else "minimum"


_FOOTER = (
    "\n\nDet er ikke en fejl. Sådan lærer integrationen hvert anlæg at kende, og det sker kun, "
    "når anlægget ikke gør, som det får besked på. Følg med i sensoren *Follow Me-indlæring* "
    "eller i AC-kortet under *Smart styring*."
)


def _check_text(cond: str, what: str, c: dict) -> str | None:
    past, short = _words(c)
    if cond == "stop" and what == "stop_level":
        return (
            f"Rummet er {_n(c['room'])} °C og målet {_n(c['target'])} °C, så anlægget har fået besked på at "
            f"stoppe: Follow Me fortæller det, at rummet er {_told(c, c['stop'])}° {past} dets mål ({_n(c['sent'], 0)} °C). "
            f"Men det kører stadig ({_n(c['power'], 0)} W).\n\n"
            "**Hvorfor:** Hvert anlæg har sin egen grænse for, hvornår det slukker. Mange Midea-anlæg slukker "
            f"først, når rummet er 2° {past} deres mål – så kører de videre forbi målet.\n\n"
            f"**Hvad sker der:** Integrationen venter op til {c['left']} min mere. Kører anlægget stadig, får det "
            f"fremover at vide, at rummet er {c['value']}° {past} målet, når det skal stoppe – så slukker det på "
            "målet. Det gemmes og huskes, også efter en genstart."
        )
    if cond == "stop" and what == "park":
        return (
            f"Anlægget kører stadig, selv om det har fået at vide, at rummet er {_told(c, c['stop'])}° {past} dets "
            f"mål. Venter {c['left']} min mere – kører det stadig, sættes anlæggets eget mål til {_parked(c)}, hver gang det "
            "skal stoppe, og tilbage ved næste start. Tjek gerne, at IR-dioden sidder rigtigt."
        )
    if cond == "hold":
        return (
            f"Anlægget har fået at vide, at rummet er {c['stop'] - 1}° {past} dets mål ({_n(c['sent'], 0)} °C), og "
            f"skulle køre roligt videre. Men det er slukket, mens rummet er {_n(c['room'])} °C "
            f"(mål {_n(c['target'])} °C).\n\n"
            f"**Hvorfor:** Det lærte stop-niveau ({c['stop']}°) er sandsynligvis en grad for højt – anlægget slukker "
            "allerede en grad tidligere.\n\n"
            f"**Hvad sker der:** Står det stadig stille om {c['left']} min, sænkes stop-niveauet til {c['value']}°."
        )
    if cond == "start":
        return (
            f"Rummet er {_n(c['room'])} °C, {_n(c['short'])} °C {short} målet {_n(c['target'])} °C, og anlægget har "
            f"fået besked på at starte: Follow Me fortæller det, at rummet er {_told(c, c['start'])}° {short} dets mål "
            f"({_n(c['sent'], 0)} °C). Men det står stille.\n\n"
            f"**Hvorfor:** Nogle anlæg starter først, når rummet er længere {short} deres mål.\n\n"
            f"**Hvad sker der:** Starter det ikke inden for {c['left']} min, får det fremover at vide, at rummet er "
            f"{c['value']}° {short} målet, når det skal starte. Det gemmes og huskes."
        )
    return None


def _result_text(cond: str, what: str, c: dict) -> str:
    past, short = _words(c)
    if what == "park":
        return (
            f"Anlægget slukker ikke, selv når det får at vide, at rummet er {c['stop']}° {past} dets mål. Når det "
            f"skal stoppe, sættes dets eget mål nu til {_parked(c)} og tilbage ved næste start, så det alligevel slukker. "
            "Tjek gerne, at IR-dioden sidder rigtigt."
        )
    if what == "start_level":
        return (
            f"Anlægget startede ikke, da det fik at vide, at rummet var {c['old']}° {short} dets mål. Fremover får "
            f"det at vide, at rummet er {c['value']}° {short}, når det skal starte – så starter det, når rummet er "
            f"{_n(c['start_at'], 2).rstrip('0')} °C {short} målet."
        )
    if cond == "stop":
        return (
            f"Anlægget blev ved med at køre, efter det fik at vide, at rummet var {c['old']}° {past} dets mål. "
            f"Fremover får det at vide, at rummet er {c['value']}° {past}, når det skal stoppe – så slukker det, "
            f"når rummet er {_n(c['stop_at'], 2).rstrip('0')} °C {past} målet."
        )
    return (
        f"Anlægget slukkede, hvor det skulle køre roligt videre. Stop-niveauet er sænket fra {c['old']}° til "
        f"{c['value']}°, så det kører roligt videre ved målet."
    )


def notify_check(hass: HomeAssistant, entry_id: str, name: str, cond: str, what: str, context: dict) -> None:
    text = _check_text(cond, what, context)
    if text is None:
        return
    persistent_notification.async_create(
        hass, text + _FOOTER, title=f"{name}: lærer anlægget at kende", notification_id=f"qlima_control_learning_{entry_id}"
    )


def notify_result(hass: HomeAssistant, entry_id: str, name: str, cond: str, what: str, context: dict) -> None:
    persistent_notification.async_create(
        hass,
        _result_text(cond, what, context)
        + "\n\n✓ Gemt – det huskes, også efter en genstart. Integrationen holder fortsat øje og retter igen, hvis "
        "anlægget ændrer opførsel. Knappen *Nulstil Follow Me-indlæring* starter forfra.",
        title=f"{name}: Follow Me-indlæring – lært",
        notification_id=f"qlima_control_learning_{entry_id}",
    )


def dismiss_learning(hass: HomeAssistant, entry_id: str) -> None:
    persistent_notification.async_dismiss(hass, f"qlima_control_learning_{entry_id}")


def notify_no_echo(hass: HomeAssistant, entry_id: str, name: str, reported, sent, minutes: int) -> None:
    persistent_notification.async_create(
        hass,
        f"Anlægget viser {_n(reported, 0)} °C, men har fået Follow Me {_n(sent, 0)} °C i {minutes} min. "
        "IR-signalet når sandsynligvis ikke frem (IR-dioden er flyttet, tildækket eller defekt), så anlægget "
        "styrer efter sin egen føler oppe under loftet. Har anlægget aldrig vist Follow Me-værdien, melder det "
        "den måske slet ikke tilbage – så kan styringen ikke bekræfte den.\n\n"
        "Indlæringen er sat på pause, så der ikke læres forkerte værdier. Beskeden forsvinder af sig selv, "
        "når signalet når frem igen.",
        title=f"{name}: Follow Me når ikke frem",
        notification_id=f"qlima_control_no_echo_{entry_id}",
    )


def dismiss_no_echo(hass: HomeAssistant, entry_id: str) -> None:
    persistent_notification.async_dismiss(hass, f"qlima_control_no_echo_{entry_id}")


def notify_unconfirmed(hass: HomeAssistant, entry_id: str, name: str, no_sensor: bool) -> None:
    """Follow Me has not been confirmed for an hour: learning waits – say why, once."""
    why = (
        "Anlæggets indetemperatur kan ikke læses (sensoren *Indetemperatur anlæg* mangler eller er "
        "utilgængelig), så styringen kan ikke se, om anlægget får den Follow Me-værdi, den sender."
        if no_sensor else
        "Anlægget melder ikke den Follow Me-værdi tilbage, det får – enten når IR-signalet ikke frem, "
        "eller anlægget melder den på en anden måde."
    )
    persistent_notification.async_create(
        hass,
        f"{why}\n\nStyringen kører videre, men indlæringen venter, så der ikke læres forkerte værdier. "
        "Beskeden forsvinder af sig selv, når Follow Me bekræftes igen.",
        title=f"{name}: Follow Me kan ikke bekræftes",
        notification_id=f"qlima_control_no_echo_{entry_id}",
    )
