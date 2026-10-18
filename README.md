<p align="center"><img src="docs/images/icon.png" width="128" alt="Qlima Control"></p>

<h1 align="center">Qlima Control</h1>

<p align="center">
Lokal styring af Qlima- og Midea-varmepumper fra Home Assistant – uden sky.<br>
En ESP32-C3 til ca. 30 kr. erstatter WiFi-sticken i anlæggets USB-port, og integrationen gør resten.
</p>

<p align="center">
<a href="https://mrdonnii.github.io/qlima-control/"><b>Installér firmware i browseren</b></a> ·
<a href="#installation-i-home-assistant">Home Assistant</a> ·
<a href="#byg-esp32-donglen">Byg donglen</a> ·
<a href="#english">English</a>
</p>

---

## Hvad får du

**I anlægget (ESP32-firmwaren)**
- Fuld styring via anlæggets egen USB-port: tænd/sluk, varme, køl, auto, affugtning, blæser, Eco/Boost/Nat/Frostvagt 8 °C.
- **Follow Me** via en IR-diode: anlægget regulerer efter *din* rumføler i stedet for sin egen føler oppe under loftet.
- Auto-vip, lamel ét trin og display til/fra via IR – præcis som fjernbetjeningen.
- Udetemperatur fra udedelen, anlæggets egen føler, filtertimer og "anlæg svarer".
- Anlæggets **fejlkode** og **filteralarm**.
- Kommandoer der kommer tæt efter hinanden, går ikke tabt.
- Valgfri pakke til andre Midea-anlæg: anlæggets **servicedata** (kompressorens frekvens, spoler, afrimning, blæsere, udedelens effekt). Qlima S 5225/5232 svarer ikke på den.
- **Lokalt web-UI** på `http://<ip>` til styring og status – virker også uden Home Assistant.
- WiFi-opsætning uden kode: ESP'en laver sit eget netværk første gang.
- **Opdaterer sig selv**: nye firmware-udgivelser her i repoet dukker op som en opdatering i Home Assistant.

**I Home Assistant (integrationen)**
- **Anlægget holder selv målet**: det får rumtemperaturen som Follow Me og målet fra integrationen – ingen tænd/sluk og ingen tomgang på 17 °C. Uden Follow Me (eller hvis du vælger det) styrer en **rumtermostat** med hysterese, min. kørsel/pause og neutral tomgang.
- **Flere rumfølere** – gennemsnit, vægtet i %, koldeste eller varmeste. Sendes automatisk til anlægget som Follow Me.
- **Døre og vinduer**: skru ned efter 5 min, sluk efter 20 min, genoptag efter 10 min lukket – med nedtællinger.
- **Shelly / effektmåler**: "Varmer"/"Køler", varmeeffekt, COP og alarm hvis anlægget kører uden effekt.
- **Strømkontakt** (fx Shelly-relæet der forsyner anlægget): tænd/sluk, **Genstart anlæg** (30 sek. uden strøm) og valgfri automatisk genstart, hvis anlægget holder op med at svare.
- **Elpris**: varmepris pr. kWh og pris pr. time.
- **Prisstyring**: målet hæves en grad i billige perioder og sænkes en grad i dyre – ud fra prisprognosen fra Strømligning, Nord Pool, Energi Data Service m.fl.
- **Vækning**: Midea-anlæg går ned på laveste kompressortrin efter et par timers hårdt arbejde og vågner først igen efter en strømafbrydelse. Kører anlægget på lavt blus, mens rummet er for koldt, slår integrationen strømmen fra og til (valgfrit, højst 3 gange i døgnet).
- **Turbo 30 min**: anlæggets egen boost i en halv time, derefter normal drift igen.
- **COP** fra en fysisk model (Carnot med belastning), kalibreret mod Qlimas SCOP.
- **Frigiv-knap**, status ("Varmer", "Venter på start", "Dør åben"…) og "Næste skift".
- **Notifikationer** til telefonen: anlæg svarer ikke, rens filter, slukket pga. åben dør.
- Alt sættes op i Home Assistants brugerflade – og kan ændres bagefter.

> Testet med **Qlima S 5225 / SC 5225** og **S 5232 / SC 5232** (Midea-OEM med "Qlima WiFi-stick"). Andre Midea-anlæg med USB-dongleport virker sandsynligvis også – vælg "Andet Midea-anlæg".

---

## Byg ESP32-donglen

### Indkøbsliste (pr. anlæg)

| Del | Bemærkning |
|---|---|
| ESP32-C3 SuperMini (eller anden ESP32-C3) | ca. 20–30 kr. |
| USB-A han-stik / et gammelt USB-kabel | klip enden af – stikket skal i anlægget |
| Modstand 4,7 kΩ | pull-up til 5 V (vigtig – uden den svarer anlægget ikke) |
| IR-diode 940 nm, 5 mm | til Follow Me, vip og display. En diode fra en gammel fjernbetjening virker fint |
| Modstand 470 Ω (220–470 Ω) | i serie med IR-dioden |
| Krympeflex / lidt varmlim | |

### Ledningsføring

<p align="center"><img src="docs/images/wiring.png" alt="Ledningsdiagram" width="900"></p>

Samme i tekst:

```
USB-A stik (til anlægget)          ESP32-C3
──────────────────────────         ──────────────────────────
Rød    5V   ─────────────┬──────── 5V
                         │
                       4,7 kΩ
                         │
Grøn   D+  (anlæg RX) ───┴──────── GPIO6   (TX, open-drain)
Hvid   D−  (anlæg TX) ──────────── GPIO7   (RX, intern pull-up)
Sort   GND ─────────────────────── GND

ESP32-C3 GPIO10 ── 470 Ω ── (+) langt ben  [IR-diode 940 nm]  kort ben (−) ── GND

Valgfri DS18B20 (målt COP), hver på sit eget ben:
  rød → 3V3   sort → GND   gul → GPIO5 (udblæsning) / GPIO4 (indsugning)
  4,7 kΩ fra gul (data) til 3V3 – én pr. føler
```

- Farverne i USB-kabler passer ikke altid. Set forfra med plastikken nedad er benene i USB-A-stikket fra venstre: **5V, D−, D+, GND**. Mål efter med et multimeter.
- Anlæggets indgang kræver **5 V-niveau** – derfor kører ESP'ens TX som open-drain med 4,7 kΩ op til 5 V. Uden modstanden svarer anlægget ikke.
- Bytter du D− og D+, sker der intet slemt – anlægget svarer bare ikke. Byt dem så.
- **Sæt aldrig ESP'ens USB-C i en computer, mens den sidder i anlægget** (den får så 5 V fra to sider).

### IR-dioden

Anlægget tager kun imod Follow Me, vip og display via IR – ikke via USB-porten. Placér IR-dioden få millimeter fra anlæggets IR-modtager bag displayet (inde bag frontpanelet), rettet mod modtagerens kuppel og uden at dække vinduet, så fjernbetjeningen stadig virker. Fastgør med varmlim. **Sluk anlægget på strømmen før du åbner det** – hovedprintet har 230 V.

Test: tryk **Lamel ét trin** – anlægget bipper.

### Temperaturfølere til målt COP (valgfrit)

Med to DS18B20-følere måler Qlima Control den varme, anlægget faktisk afgiver: luftmængden (fra databladet efter blæsertrin) × temperaturforskellen mellem udblæsning og indsugning. Sammen med effektmåleren giver det en **målt COP** ved siden af den beregnede.

- **Udblæsning (GPIO5):** i luftstrømmen lige under lamellerne – ikke i direkte kontakt med metal.
- **Indsugning (GPIO4):** bag filteret i toppen af indedelen.
- Brug vandtætte DS18B20 med kabel eller TO-92-udgaven med krympeflex. Hver føler skal have en 4,7 kΩ fra data (gul) til 3V3.
- Firmwaren finder følerne selv – der skal ikke ændres noget. De dukker op som *Udblæsning* og *Indsugning*, og integrationen viser *Varmeeffekt (målt)* og *COP (målt)*.

### Flash firmwaren

**Nemmest:** åbn **<https://mrdonnii.github.io/qlima-control/>** i Chrome eller Edge, sæt ESP32-C3'en i computeren og tryk *Installér*. Du kan vælge WiFi direkte i installeren.

**Med ESPHome:** ESPHome finder den som "Qlima Control" og tilbyder at adoptere den. Eller brug filen [`esphome/qlima-control-esp32c3.yaml`](esphome/qlima-control-esp32c3.yaml) i dit eget ESPHome.

### WiFi og web-UI

- Første opstart uden WiFi: forbind til netværket **Qlima Control** (kode `qlimacontrol`), vælg dit WiFi på siden der åbner (eller `http://192.168.4.1`).
- Web-UI: `http://<ip-adresse>` – bruger `admin`, kode `qlima`. Her kan du styre anlægget og se status uden Home Assistant.
- Navnet får automatisk ESP'ens MAC-adresse som endelse (fx `qlima-control-2c40c8`), så flere anlæg kan stå side om side.

### Opdateringer

ESP'en tjekker repoet hver 6. time. Med kontakten **"Opdatér automatisk"** (slået til som standard) installerer den selv en ny firmware over WiFi. Slår du den fra, viser Home Assistant i stedet **"Firmware – opdatering tilgængelig"**, og du trykker selv *Installér*. Firmwaren udgives kun, når der faktisk er ændret noget i den.

Hver Qlima Control-enhed har også en **Firmware**-entitet, der viser hvilken version ESP'en kører, og om der er en nyere udgivelse. *Installér* er kun muligt, når ESP'en selv bekræfter, at den kører det færdige image. Adopterede enheder opdateres i ESPHome Builder.

> **Adopteret i ESPHome?** Så er din WiFi-kode bygget ind i firmwaren, og du skal altid opdatere fra ESPHome Builder. Online-opdateringen (det færdige image) kender ikke din WiFi. Fra 1.7.6 får adopterede enheder den ikke med; byg ældre installationer én gang fra ESPHome Builder for at fjerne den. Et enhedsnavn, der ligner fabriksnavnet, giver ikke adgang til det færdige image. Ældre fabriksimages uden markøren kan opdateres via ESP'ens egen Firmware-entitet eller webinstalleren, indtil de kører firmware 1.3.3.

**Mister ESP'en WiFi**, åbner den efter ca. 1 minut hotspottet **"Qlima Control"** (kode `qlimacontrol`). Forbind med telefonen, og vælg WiFi på siden der åbner (eller http://192.168.4.1). Den prøver det gamle netværk i baggrunden imens. Bygger du selv, så lad være med at sætte `project:` i din yaml; versionen kommer fra den fælles pakke.

Integrationen opdateres gennem HACS som alle andre.

---

## Installation i Home Assistant

1. **HACS** → ⋮ → *Brugerdefinerede repositories* → `https://github.com/MRDonnii/qlima-control` → kategori *Integration*. Installér **Qlima Control** og genstart Home Assistant.
2. Sørg for at ESP'en er tilføjet i **ESPHome**-integrationen (den bliver normalt fundet automatisk).
3. **Indstillinger → Enheder og tjenester → Tilføj integration → Qlima Control**.
4. Vælg ESP'en i anlægget og kobl rummets sensorer på:

| Sektion | Felter |
|---|---|
| **Anlæg** | navn, ESP i anlægget, model (5225 / 5232 / andet Midea) |
| **Rumfølere** | rumtemperatur (én eller flere + gennemsnit/vægtet/koldeste/varmeste), fugtighed, udetemperatur, elpris (Strømligning, Nord Pool, Energi Data Service, Tibber …) |
| **Strøm og Shelly** | effektmåler, energimåler, strømkontakt (Shelly-relæ), automatisk genstart, "kører over" W, maks. effekt |
| **Døre og vinduer** | sensorer, skru ned efter, sluk efter, genoptag efter, skru ned til målet eller minimum |
| **Styring** | mål ved varme/køl, start- og stop-hysterese, min. kørsel/pause, manuel overstyring, tilstedeværelse og sænkning når I er væk |
| **Notifikationer** | notify-tjeneste (fx `notify.mobile_app_telefon`), filter-påmindelse |
| **Avanceret** | termostater der skal pauses, følerfrister |

**Bagefter** kan alt justeres under *Indstillinger → Enheder og tjenester → Qlima Control → Konfigurer*: en menu hvor du vælger én af grupperne ovenfor (også ESP'en og modellen).

Vælger du **vægtet**, får du et trin hvor hver føler får en procent. En føler der er offline udelades, og de andre deler dens vægt.

### Enheder

| Enhed | |
|---|---|
| `climate` | rumtermostaten – brug denne på dine dashboards |
| Status, Næste skift (+ nedtællinger under Diagnostik) | hvad den gør nu, og hvornår den skifter |
| Rumtemperatur, Rumfugtighed, Udetemperatur, Anlæggets føler | |
| Elforbrug, Energi, COP, Varmeeffekt, Varmepris, Pris pr. time | kræver effektmåler/elpris |
| Energi i dag/denne måned, Pris i dag/denne måned | gemmes og overlever genstart |
| Driftstid i dag, Starter i dag, Afrimninger i dag | afslører om kompressoren tænder og slukker for tit |
| Udblæsning, Indsugning, Varmeeffekt (målt), COP (målt) | kræver DS18B20-følerne |
| Varmer, Køler, Afrimer, Dør/vindue åben, Anlæg svarer, Kører uden effekt | binære sensorer |
| Frigiv, Sving til/fra, Lamel ét trin, Display, Nulstil filtertæller, Genstart anlæg (strøm) | knapper |
| Follow Me, Bip, Strøm til anlæg, Reagér på døre og vinduer, Prisstyring, Vækning ved slumring, Turbo 30 min | kontakter |
| Dør/vindue: skru ned efter, sluk efter, genoptag efter | tal (minutter) – ændres med det samme |
| Pris: op når billigt, Pris: ned når dyrt | tal (hele grader, 0–2) – ændres med det samme |
| Prisniveau, Vækning | hvad prisstyringen og vækningen gør lige nu |
| ESP32-status | Forbundet / Anlægget svarer ikke / Ny firmware klar / Ikke forbundet – med firmware, ESPHome, IP, MAC, WiFi og oppetid |
| ESP32 forbundet, WiFi-signal, IP-adresse, oppetid, ESPHome-version | ESP'ens egen diagnostik samlet på Qlima Control-enheden |
| Firmware | ESP'ens firmwareversion og om den er den nyeste – med *Installér*, når der er en ny |
| Program | anlæggets Eco/Boost/Nat/Frostvagt – kun dem anlægget tager i den aktuelle tilstand (Normal, når det er slukket; Eco kun ved køl og auto) |

### Sådan regulerer den

*Indstillinger → Styring → Regulering*:

- **Anlægget holder målet (Follow Me)** – standard. Anlægget får rumtemperaturen som Follow Me og selve rummålet (inkl. fravær og prisstyring) og modulerer selv kompressoren. Integrationen skruer kun ned ved åben dør/vindue (altid til minimum) og slukker, hvis den står åben længe. Termostaten viser *inaktiv*, når anlægget holder målet uden at køre. Er Follow Me slået fra, eller mangler rumtemperaturen, bruges termostaten nedenfor.
  - Follow Me kan kun sende **hele grader**, og hvert anlæg har sine egne grænser for, hvornår det starter og slukker (en S 5225 slukker først, når den får at vide, at rummet er 2° over dens mål, og starter ved 1° under). Med den rigtige rumtemperatur ville rummet svinge op til 2 °C. Derfor styrer integrationen den værdi, der sendes, ud fra rumføleren (0,1 °C), mens anlæggets eget mål står fast (målet afrundet – mål i **halve grader** som 21,5 °C kan vælges):
    - **Start** når rummet er 0,2 °C under målet: anlægget får at vide, at rummet er mindst dets *start-niveau* under målet (rumtemperaturen, når den er længere væk) – fuld varme, indtil målet er nået.
    - **Hold** fra målet og op til +0,3 °C: en grad før *stop-niveauet* – et kørende anlæg skruer ned og kører roligt videre, et slukket bliver slukket.
    - **Stop** ved +0,3 °C: anlægget får at vide, at rummet er mindst dets *stop-niveau* over målet, så kompressoren slukker – indtil rummet igen er 0,2 °C under målet.
    - Min. kørsel (20 min) og pause (10 min) måles på selve kompressoren og gælder også her, medmindre rummet er 0,5 °C forbi målet. Termostatens nedtællinger og status *venter på start* viser dem.
  - **Indlæring.** Niveauerne læres af, hvad anlægget gør (kompressoren, ellers effektmåleren) – og kun når anlægget samtidig melder den Follow Me-værdi tilbage, det har fået (det viser den som sin indetemperatur):
    - kører det stadig 6 min efter *stop* (ud over dets egen min. kørsel) → stop-niveauet hæves en grad;
    - slukker det i *hold*, hvor det skulle køre videre, i 6 min (12 min i frostvejr, hvor det ligner en afrimning) → stop-niveauet sænkes – var niveauet nødvendigt for at stoppe, først anden gang i træk;
    - starter det ikke inden for 10 min i *start* → start-niveauet hæves en grad;
    - stiger rummet ikke i 15 min, mens det kører → kun denne start skubbes en grad;
    - slukker det ikke engang ved 5° → dets eget mål sættes til minimum (maksimum ved køl) under stop og tilbage ved næste start.
    - Der læres ikke uden effektmåler/kompressorfrekvens, når Follow Me ikke bekræftes (også hvis anlæggets indetemperatur mangler – efter en time siger den det), under afrimning, ved fejlkode eller offline. Melder anlægget værdien forskudt med en fast grad, læres forskydningen af tre værdier. Varme og køl læres hver for sig, og det gemmes for sig selv (også efter genstart). Knappen *Nulstil Follow Me-indlæring* starter forfra.
    - **Vejret:** for hvert 5 °C-interval ude måler den, hvor meget rummet stadig stiger efter start-trinnet og falder efter en start, og rykker stop (ned til +0,15 °C) og start (ned til −0,1 °C), så rummet holder sig om målet både i frost og i mildt vejr.
    - **Sådan ses det:** sensoren *Follow Me-indlæring* (og AC-kortet under *Smart styring*) viser stop- og start-niveau, om de er bekræftet, om Follow Me når frem, hvad vejret har lært, hvilket tjek der kører, hvor langt det er, og de seneste ti ting den har lært. Varer et tjek længere, end et anlæg normalt er om at reagere, kommer der en besked i Home Assistant, der forklarer, hvad der sker og hvorfor – og en ny, når den har lært noget. Melder anlægget ikke Follow Me tilbage i 10 min, siger den det (IR-dioden).
  - Køl er spejlvendt. Termostaten viser det som `follow_me_step` og `follow_me_sent`; *Rumtemperatur (modtaget)* på ESP'en viser den styrede værdi.
- **Termostaten** – varmepumpen starter, når rummet er **under målet** (start-hysterese, standard 0,1 °C), og holder i tomgang når rummet er **målet + 0,5 °C** – anlæggets setpunkt sænkes til minimum i stedet for at slukke, så kompressoren skånes. Uden Follow Me kører Midea-anlæg ellers typisk videre til ca. 2 °C over målet, fordi de måler oppe under loftet.
- Ændrer nogen noget på fjernbetjeningen, respekteres det som **manuel overstyring** (standard 120 min). **Frigiv** afslutter den med det samme.
- Slukker man anlægget på fjernbetjeningen, slukker termostaten også.

### Begrænsninger

- Anlæggene tager kun **hele grader** (også Follow Me) – halve grader i termostaten opnås ved at styre Follow Me ud fra rumføleren.
- Swing/vip styres via IR, så Home Assistant kan ikke se om anlægget vipper.
- Qlima S 5225/5232 svarer kun med Mideas korte status – ingen kompressorfrekvens, spoletemperaturer eller effekt fra anlægget selv (testet med tre rammeformater). Brug en effektmåler. Andre Midea-anlæg kan give servicedata via `qlima-control-servicedata.yaml`.

---

### Fravær

Kobl en person, en gruppe af personer eller alarmen på under *Styring*. Når ingen er hjemme (eller alarmen er slået til "ude"), sænkes målet ved varme med f.eks. 3 °C og hæves tilsvarende ved køl.

### Prisstyring

Slå *Prisstyring* til (kontakt eller *Indstillinger → Pris og vækning*). Den aktuelle pris rangeres mod de kendte perioder fra 6 timer tilbage til et døgn frem: blandt de billigste 25 % hæves varmemålet 1 °C, blandt de dyreste 25 % sænkes det 1 °C (køl: omvendt). Niveauet holdes mindst 30 minutter, så anlægget ikke får nyt setpunkt hele tiden. Prisperioderne kan være kvarter eller time – eller følge en select, der siger "15 min"/"time".

### Vækning

Midea-anlæg (og dermed Qlima) kører efter deres egen sparepolitik: efter et par timers hårdt arbejde går de ned på laveste kompressortrin og bliver der, selv om rummet ikke når målet – og turbo bliver ignoreret i den tilstand. En strømafbrydelse nulstiller det. Er *Vækning ved slumring* slået til, og rummet er mindst 0,7 °C under målet, mens anlægget bruger under 300 W (eller kompressoren står på højst 25 Hz, når anlægget sender servicedata) i 45 minutter, slår integrationen strømmen fra i 30 sekunder og til igen. Den gør det kun, når termostaten vil have anlægget til at køre, aldrig under afrimning, højst 3 gange i døgnet og med 2 timer imellem. Efter strømafbrydelsen sættes anlæggets tilstand automatisk på plads igen – den tolkes ikke som et tryk på fjernbetjeningen. Grænserne kan ændres under *Pris og vækning*. Iagttagelserne bag stammer fra [citi94/midea-homekit-dongle](https://github.com/citi94/midea-homekit-dongle) (FINDINGS.md).

### Servicemenuen på fjernbetjeningen

Hold **On/Off + Fan** inde i 8 sekunder: displayet viser koder med anlæggets egne målinger (T1–T4, kompressorens Hz, strøm og spænding). Godt til at tjekke COP-modellen og om anlægget slumrer.

### Hjælp og fejlsøgning

- **Diagnose:** *Indstillinger → Enheder og tjenester → Qlima Control → ⋮ → Download diagnose* – vedhæft filen, når du beder om hjælp.
- **Reparationer:** Home Assistant viser en advarsel under *Indstillinger → Reparationer*, hvis ESP'ens firmware er for gammel, rumfølerne mangler, eller ESP'en ikke kan findes.

## Fejlfinding

| Problem | Løsning |
|---|---|
| ESP'en kommer ikke på WiFi | Mange C3 SuperMini har en svag antenne – firmwaren kører med 8,5 dBm. Flyt tættere på routeren. |
| "Anlæg svarer" er slukket | Mangler 4,7 kΩ pull-up, eller D−/D+ er byttet. |
| Follow Me/vip virker ikke | Kontrollér at IR-dioden blinker (telefonkamera) og sidder tæt på modtageren. Prøv 220 Ω. |
| Varmer for længe | Brug termostaten fra integrationen (ikke anlæggets egen `climate`). |
| Opsætningen siger "ingen klimaenhed" | ESP'en kører ikke Qlima Control-firmwaren, eller den er ikke tilføjet i ESPHome. |

---

## English

Qlima Control gives you **local control of Qlima/Midea split heat pumps** (Qlima S/SC 5225 and 5232 tested) from Home Assistant. An ESP32-C3 replaces the unit's WiFi stick in its USB port (Midea UART at 9600 baud, 5 V open-drain with a 4.7 kΩ pull-up), and an IR LED next to the unit's receiver handles Follow Me, swing and display. Flash it from the [web installer](https://mrdonnii.github.io/qlima-control/), add it in ESPHome, then add the **Qlima Control** integration (HACS custom repository) and link room temperature sensors (mean, weighted %, min or max), humidity, a power meter, electricity price, doors and windows and a notify service. The unit holds the room target itself on Follow Me (or a room thermostat with hysteresis takes over), with door/window staging, Follow Me, COP and heat price, filter reminders, release/status/countdowns and all unit functions as buttons. Price steering moves the target a degree up in cheap and down in expensive periods (Strømligning, Nord Pool, Energi Data Service and other forecasts), an optional wake-up power-cycles the unit when it idles in its low-effort hold while the room is too cold (Midea units only work hard again after a power cycle), and Turbo runs the unit's boost for 30 minutes. The firmware reads the unit's fault code and filter alarm, no longer drops commands sent close together (an optional package reads service data from Midea units that answer it), has a local web UI and updates itself from this repository's releases.

---

Lavet af MRDonnii · MIT-licens · ikke tilknyttet Qlima eller Midea.
