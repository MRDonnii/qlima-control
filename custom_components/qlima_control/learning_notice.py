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


def _signed(value) -> str:
    value = int(value or 0)
    return f"{'+' if value > 0 else '−' if value < 0 else ''}{abs(value)}°"


_FOOTER = (
    "\n\nDet er ikke en fejl. Sådan lærer integrationen hvert anlæg at kende, og det sker kun, "
    "når anlægget ikke gør, som det får besked på. Følg med i sensoren *Follow Me-indlæring* "
    "eller i AC-kortet under *Smart styring*."
)


def _check_text(cond: str, what: str, c: dict) -> str | None:
    if cond == "stop":
        return (
            f"Rummet er {_n(c['room'])} °C og målet {_n(c['target'])} °C, så anlægget har fået besked på at "
            f"stoppe (Follow Me {_n(c['sent'], 0)} °C). Det kører stadig ({_n(c['power'], 0)} W).\n\n"
            "**Hvorfor:** Mange Midea-anlæg stopper først, når de får at vide, at rummet er 2 grader over deres "
            "mål, og nogle lægger selv lidt til Follow Me-værdien. Så kører de videre over målet.\n\n"
            f"**Hvad sker der:** Integrationen venter op til {c['left']} min mere. Kører anlægget stadig, sender "
            f"den fremover en grad højere værdi ved stop (rettelse {_signed(c['value'])}), så anlægget slukker "
            "på målet. Det gemmes og huskes, også efter en genstart."
        ) if what == "offset_up" else (
            f"Anlægget kører stadig, selv om det har fået at vide, at rummet er {c['offset'] + 1} grader over målet. "
            f"Venter {c['left']} min mere – kører det stadig, sænkes anlæggets eget mål til minimum, hver gang det "
            "skal stoppe, og sættes tilbage ved næste start. Tjek gerne, at IR-dioden sidder rigtigt."
        )
    if cond == "hold":
        return (
            f"Anlægget har fået at vide, at rummet er på målet (Follow Me {_n(c['sent'], 0)} °C), og skulle køre "
            f"roligt videre. Men det er stoppet, mens rummet er {_n(c['room'])} °C (mål {_n(c['target'])} °C).\n\n"
            f"**Hvorfor:** Den lærte rettelse ({_signed(c['offset'])}) er sandsynligvis en grad for stor til dette "
            "anlæg, så det tror, rummet er for varmt.\n\n"
            f"**Hvad sker der:** Står det stadig stille om {c['left']} min, sænkes rettelsen til {_signed(c['value'])}."
        )
    if cond == "start":
        action = (
            f"sendes startsignalet {c['value']}° lavere fremover, så anlægget starter allerede 0,2 °C under målet"
            if what == "start_earlier"
            else f"sænkes rettelsen til {_signed(c['value'])}, fordi den er for stor til, at anlægget vil starte"
        )
        return (
            f"Rummet er {_n(c['room'])} °C, {_n(c['short'])} °C under målet {_n(c['target'])} °C, og anlægget har "
            f"fået besked på at starte (Follow Me {_n(c['sent'], 0)} °C). Men det står stille.\n\n"
            "**Hvorfor:** Nogle anlæg starter først, når de får at vide, at rummet er længere under deres mål.\n\n"
            f"**Hvad sker der:** Starter det ikke inden for {c['left']} min, {action}. Det gemmes og huskes."
        )
    if cond == "rise" and what == "offset_down":
        return (
            f"Anlægget kører, men rummet stiger ikke ({_n(c['room'])} °C, mål {_n(c['target'])} °C).\n\n"
            f"**Hvorfor:** Den lærte rettelse ({_signed(c['offset'])}) er måske for stor, så anlægget tror, at "
            "rummet næsten er varmt nok, og arbejder for svagt.\n\n"
            f"**Hvad sker der:** Stiger rummet ikke inden for {c['left']} min, sænkes rettelsen til {_signed(c['value'])}."
        )
    return None


def _result_text(cond: str, what: str, c: dict) -> str:
    if what == "offset_up":
        return (
            f"Anlægget blev ved med at køre {c['minutes']} min efter, at det fik besked på at stoppe. Fremover "
            f"sendes Follow Me {c['value']}° højere ved stop og hold (rettelse {_signed(c['value'])}), så det "
            "slukker, når rummet er 0,3 °C over målet."
        )
    if what == "park":
        return (
            f"Anlægget reagerer ikke på Follow Me, selv når det får at vide, at rummet er {c['offset'] + 1} grader "
            "over målet. Når det skal stoppe, sættes dets eget mål nu ned til minimum og tilbage ved næste start, "
            "så det alligevel slukker. Tjek gerne, at IR-dioden sidder rigtigt."
        )
    if what == "start_earlier":
        return (
            f"Anlægget startede ikke på det normale startsignal. Fremover sendes startsignalet {c['value']}° lavere, "
            "så anlægget starter, når rummet er 0,2 °C under målet."
        )
    why = {
        "hold": "Anlægget stoppede ved målet, hvor det skulle køre roligt videre",
        "start": "Anlægget ville ikke starte med den lærte rettelse",
        "rise": "Rummet steg ikke, fordi anlægget arbejdede for svagt",
    }.get(cond, "Anlægget gjorde ikke, som det fik besked på")
    return f"{why}. Rettelsen er sænket til {_signed(c['value'])}."


def notify_check(hass: HomeAssistant, entry_id: str, name: str, cond: str, what: str, context: dict) -> bool:
    text = _check_text(cond, what, context)
    if text is None:
        return False
    persistent_notification.async_create(
        hass, text + _FOOTER, title=f"{name}: lærer anlægget at kende", notification_id=f"qlima_control_learning_{entry_id}"
    )
    return True


def notify_result(hass: HomeAssistant, entry_id: str, name: str, cond: str, what: str, context: dict) -> None:
    text = _result_text(cond, what, context)
    persistent_notification.async_create(
        hass,
        text + "\n\n✓ Gemt – det huskes, også efter en genstart. Integrationen holder fortsat øje og retter "
        "igen, hvis anlægget ændrer opførsel. Knappen *Nulstil Follow Me-indlæring* starter forfra.",
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
        "styrer efter sin egen føler oppe under loftet.\n\n"
        "Indlæringen er sat på pause, så der ikke læres forkerte værdier. Beskeden forsvinder af sig selv, "
        "når signalet når frem igen.",
        title=f"{name}: Follow Me når ikke frem",
        notification_id=f"qlima_control_no_echo_{entry_id}",
    )


def dismiss_no_echo(hass: HomeAssistant, entry_id: str) -> None:
    persistent_notification.async_dismiss(hass, f"qlima_control_no_echo_{entry_id}")
