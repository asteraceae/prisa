# External data sources

We need to check whether we are allowed to use on commercial lisences or not.

## Telefónica (primary data)

Spec: "Descripción de los Datasets actualizada" (14 Oct 2026), linked from
https://www.upf.edu/web/catedra-telefonica/mobiledatachallenge; a copy is in the
course folder as `Smart Steps - Descripcion datasets actualizada 2026-10-14.pdf`.

**Access:** **not public**. Only teams selected for Phase 2 (announced 23 Oct) get
the full data, from 24 Oct, after every team member signs a confidentiality
agreement. It must never be committed or shared outside the team.

## Geography and population Data

Really the first one is the most important as it has the same grain as the telefonica data. The tourist intensity one is also worth looking at.

| Dataset | Publisher | URL | Grain | Period | Licence |
|---|---|---|---|---|---|
| Districtes i barris | Open Data BCN | https://opendata-ajuntament.barcelona.cat/data/dataset/20170706-districtes-barris | 10 districts, 73 barris, census sections; WKT in ETRS89 (area from this) | current | CC BY 4.0 |
| Padró `pad_mdbas_sexe` | Open Data BCN | https://opendata-ajuntament.barcelona.cat/data/dataset/pad_mdbas_sexe | census section → barri, yearly at 1 Jan | 1997–2026 | CC BY 4.0 |
| Population projection | Open Data BCN | https://opendata-ajuntament.barcelona.cat/data/dataset/poblacio_projectada | city | base 2024 | CC BY 4.0 |
| Tourist flats register `habitatges-us-turistic` | Open Data BCN | https://opendata-ajuntament.barcelona.cat/data/dataset/habitatges-us-turistic | flat, places, barri, lat/lon; quarterly | 2018 Q2 – 2026 Q3 | CC BY 4.0 |
| Hotels / pensions / other accommodation | Open Data BCN | https://opendata-ajuntament.barcelona.cat/data/dataset/allotjaments-hotels | points, no bed counts | updated Oct 2023 | CC BY 4.0 |
| Tourist intensity layers (2022, grid) | Open Data BCN / Barcelona Regional | https://opendata-ajuntament.barcelona.cat/data/dataset/intensitat-activitat-turistica | grid, inputs 2016–2019 | one-off | CC BY 4.0 |
| Informe de l'activitat turística 2024 | Observatori del Turisme a Barcelona | https://ajuntament.barcelona.cat/turisme/sites/default/files/2026-03/2024_IAT_OTB_1.pdf | visitors per attraction; hotel places per district | 1994–2024 | not stated |

Barri codes: the official number runs 01–73 across the whole city (la
Barceloneta 03, el Fort Pienc 05). Telefónica's real code is 9 digits,
08019 + district + position *within* the district (el Bon Pastor is 080190903,
officially 59). `data/reference/barri_crosswalk.csv` maps one to the other and
was checked by name against all 73 barris in the delivery.

## Transport supply

The top one is exactly what we were looking for, it only needs attribution to its licence so its good.

| Dataset | Publisher | URL | Licence |
|---|---|---|---|
| ATM GTFS, all operators in Catalonia (best single source) | ATM via the ministry's national access point (NAP) | https://nap.transportes.gob.es/api/Fichero/download/2060 (registration/API key) | Ministry open licence, commercial OK, "Powered by MITRAMS" |
| TMB GTFS + iBus/iMetro real time | TMB | https://developer.tmb.cat (app_id/app_key) | TMB API terms: only for the app you declare |
| TMB GTFS, public mirror (what we use) | Mobility Database (mdb-2359), copy of TMB's official feed | https://mobilitydatabase.org/feeds/gtfs/mdb-2359 | TMB developer terms still apply: don't redistribute the raw feed |
| FGC GTFS + GTFS-RT | FGC | https://www.fgc.cat/google/google_transit.zip | CC BY 4.0 |
| Rodalies GTFS / GTFS-RT | Renfe | https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip; https://gtfsrt.renfe.com/ | commercial OK, attribution |
| TRAM GTFS | TRAM | https://opendata.tram.cat/GTFS/zip/TBX.zip, TBS.zip | unverified: the portal only links generic terms of use |
| AMB bus GTFS | AMB | https://www.ambmobilitat.cat/OpenData/google_transit.zip | not stated for the static file; AMB's GTFS-RT says reuse under Llei 37/2007 (attribution + date), probably the same |
| Bus stops, funiculars, cable cars | Open Data BCN | https://opendata-ajuntament.barcelona.cat | CC BY 4.0 |

Vehicle capacities are in `data/reference/vehicle_types.csv` with a source per row.
TMB averages from its 2025 institutional presentation (2024 data): 206 places per
metro car (about 1,030 per 5-car train) and 86 places per bus. The standing
density behind TMB's "places" figure is not published, so we state our own
(4 or 6 people per m²) in any result.

## Ridership and load (calibration)

No open dataset gives hourly load per station in Barcelona!!!. We might need to send an email to ATM or TMB directly.

| Dataset | Publisher | Grain | Period | Licence |
|---|---|---|---|---|
| Metro riders per station and line, monthly totals and average weekday | TMB transparency portal | station x month | 2021–2025 | **non-commercial only** |
| Bus riders per line, monthly totals and average weekday (includes line 116) | TMB transparency portal | line x month | 2021–2025 | **non-commercial only** |
| Occupancy levels on the web and app (5 levels, by line, direction, station and time slot) | TMB | – | since May 2020 | no API; tmb.cat terms |
| Monthly demand per station | FGC / Generalitat (dataset 4z2p-zzwm) | station x month | 2019–2025 | CC BY 4.0 |
| Geotren: live train positions with % occupancy per car | FGC (dadesobertes.fgc.cat, posicionament-dels-trens) | train x real time | live only; **start logging now** | CC BY 4.0 |
| Riders per 30-minute slot, Barcelona commuter network | Renfe (data.renfe.com) | station x 30 min, boardings and alightings | 2018 | CC BY 4.0 |
| Interurban bus passengers per line, yearly | Generalitat (xgb3-3duu) | line x year | 2012–2023 | Generalitat open licence |
| Public transport incidents | Generalitat (b8ba-rfcf) | event | to Oct 2026 | Generalitat open licence |

Key figure: T-usual is 70.7% of T-mobilitat validations (ATM press release,
Mar 2024). Pass woulfn't pay for our ticket so the price only affects pay-per-trip travellers.

## Hourly and post-Aug-2025 mobility (to buttress the model cause how the fuck do you have a dataset that ends in 2025 bro!?)

| Dataset | Publisher | Grain | Period | Licence |
|---|---|---|---|---|
| Big Data mobility study (`movilidad-opendata.mitma.es`) | Ministry of Transport (MITMA) | **hourly** OD; Barcelona at 10-district level only; Spanish residents only; activity at destination (`no_frecuente` is a proxy for visits); overnight stays | daily Jan 2022 – Jun 2025; after that sample days only (8–10 and 14–17 Aug, 13–19 Oct, 6–8 Dec 2025); methodology break 1 Jul 2025 | MITMA open licence, commercial OK, "Basado en datos abiertos MITMA" |
| [Tourism measured with mobile phones (experimental)](https://www.ine.es/experimental/turismo_moviles/experimental_turismo_moviles.htm) | INE | municipality x month | inbound ends Dec 2025 (moves into FRONTUR); domestic continues | CC BY 4.0 |
| Hotel and tourist-apartment occupancy (EOH, EOAT) | INE | Barcelona x month | ongoing | CC BY 4.0 |
| Traffic and bike counters `aforaments-detall` | Open Data BCN | ~880 counters x month (average daily by day type) | 2017–2025 | CC BY 4.0 |
| Bicing station status | Open Data BCN | station x minutes | to 2026 | CC BY 4.0 |

## Calendar and events

| Dataset | Publisher | Notes | Licence |
|---|---|---|---|
| Local holidays of Catalonia (b4eh-r8up) | Generalitat | per municipality; contains junk rows; 2027 not loaded yet, so 17 May and 24 Sep 2027 are assumed (betevé reports 17 May was chosen) | Generalitat open licence |
| Catalan holidays 2027 (Ordre EMT/52/2026) | Generalitat | 1 Jan, 6 Jan, 26 Mar, 29 Mar, 1 May, 24 Jun, 11 Sep, 12 Oct, 1 Nov, 6 Dec, 8 Dec, 25 Dec. Barcelona local holidays for 2027 not published yet | – |
| School calendar 2026-27 (Ordre EDF/66/2026) | Generalitat | 8 Sep 2026 – 21 Jun 2027; Christmas 22 Dec – 7 Jan; Easter 20–29 Mar | – |
| Concerts at large venues `recintes-gran-aforament` | Open Data BCN | 2023–2025 | CC BY 4.0 |
| Weather (what we use) | Open-Meteo historical archive (ERA5), Barcelona grid cell | daily, 2023 to now | CC BY 4.0 data, but the free API is **non-commercial**; a commercial product would need AEMET or a paid plan |
| Football fixtures | openfootball/football.json | FC Barcelona LaLiga home matches 2023–2027 | public domain |
| Hand-curated events | `data/reference/events.csv` | 64 events (MWC, Primavera, Mercè, marathon...), one source URL per row | – |

Known 2027 events: MWC 1–4 Mar (Fira Gran Via, L'Hospitalet), Primavera Sound
2/3–6 Jun (Parc del Fòrum). No open consolidated events dataset seems to exist, we would have to build one by hand if we wanted to be robust.

## Diff between 2027 and the 2023-2025 period

- Methodology breaks: MITMA on 1 Jul 2025; INE inbound tourism moving into FRONTUR.

- Tourist-flat licences being phased out by Nov 2028 (unverified), and higher
  tourist-tax surcharges.
- Spotify Camp Nou capacity going up in phases after it reopened in Nov 2025.
- Gaudí centenary and the Sagrada Família towers (2026) inflate the baseline.
- Timed-ticket caps at Sagrada Família and Park Güell limit how far those sites can grow, we can work backward from this to find the # visitors.

## Mode share (`data/reference/mode_share.csv`)

None of these documents states a licence; cite them as official statistics.

| Source | What it gives | URL |
|---|---|---|
| EMEF 2024 Resum Executiu (ATM / Institut Metròpoli, Apr 2025) | Residents 16+, weekday trips: Barcelona-internal (60.1% active / 27.9% public transport incl. taxi / 12.0% private) and Barcelona↔rest of the metro area (5.2 / 52.9 / 41.9), with splits inside each group | https://recam.amb.cat/bitstreams/e2fe04ae-2308-4d10-8137-bd0dbcef19e7/download |
| AMB Informe de la mobilitat 2024 (Jul 2025) | 2024 ridership in millions: metro 468.7, FGC 97.5, Rodalies 117.7, tram 34.9, TMB bus 219.1 | https://recam.amb.cat/bitstreams/87b8cd37-2917-425b-a576-cef77b3fd385/download |
| PMU 2030 participation slides (Ajuntament, May 2024), slide 20 | Tourist trips: walk 51.0, metro 29.7, TMB bus ~4.2, rail ~1.4, other public 8.4, private 2.0 | https://www.barcelona.cat/mobilitat/sites/default/files/2024-05/240509_Sessi%C3%B3%206_Mobilitat%20i%20Sostenibilitat_Part%201.pdf |
| Perfil i hàbits dels turistes a Barcelona 2024 (OTB, Apr 2025) | Main mode of tourists: walk 44.7, metro 28.1, taxi 13.4, bus 5.6, Uber/Cabify 2.3 | https://ajuntament.barcelona.cat/turisme/sites/default/files/2025-04/Informe%20enquesta%20perfil%20turistes%20Barcelona%20ciutat%202024.pdf |
| Estratègia de mobilitat turística, Mesura de Govern (Nov 2017) | 2016 tourist trips by mode; tourists ≈15% of metro users (196k trips/day); ticket mix of tourists' trips | https://ajuntament.barcelona.cat/turisme/sites/default/files/mesura_de_govern_mobilitat.pdf |

## What the pipeline uses (Nov 2026)

| Module | Sources | Commercial use |
|---|---|---|
| `prisa.ingest` | Telefónica delivery | **no** (NDA, challenge only) |
| `prisa.external.geography` | Open Data BCN barris, padró, tourist flats; AMB municipality list | yes, attribution "Ajuntament de Barcelona" |
| `prisa.external.calendar` | `holidays` library (MIT), Generalitat local holidays, DOGC school orders, Open-Meteo, openfootball, events.csv | yes except Open-Meteo's free API |
| `prisa.external.transit` | TMB GTFS (Mobility Database mirror), FGC, Renfe, TRAM, AMB | FGC/Renfe yes; TMB developer terms; TRAM/AMB unverified |
| `prisa.external.ridership` | TMB ridership spreadsheets, FGC monthly demand, Renfe 2018 slots, our FGC Geotren archive | TMB **no** (calibration only); others yes |
| `prisa.external.mitma` | MITMA big-data mobility (hourly, districts) | yes, "Basado en datos abiertos MITMA" |
| `data/reference/mode_share.csv` | EMEF 2024, PMU 2030 slides, OTB tourist survey | cite as official statistics |
