# Varauspaneelin tuotantovalmiuden parannukset – toteutussuunnitelma

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turvallinen ja ymmärrettävä toimintatapa ruuhkassa, epävarmoissa varauksissa ja manuaalisessa jatkamisessa; todenna avaushetken toiminta paikallisessa simulaattorissa.

**Architecture:** Paneelin statusmuunnokset eivät sisällä yksityisiä backend-vastauksia. Manuaalinen varatie on vain hyväksytyn kurssin julkinen linkki ja selkeät ohjeet; se ei ohita maksunestoa, eikä tee maksua. Deterministinen TestShop ja Playwright testaavat avausviivettä sekä varauspyyntöjen määrää.

**Tech Stack:** Python 3.11, stdlib HTTP server, Playwright, pytest, GitHub Actions.

**Spec:** Käyttäjän 22.11.2026 pyyntö: korjaa tilaseuranta, turvallinen manuaalinen varatie ja avaushetken ruuhka/nopeustesti PR #8:ssa. Ei oikeita varauksia eikä maksuja.

## Global Constraints
- Vain käyttäjän hyväksymät ID:t 110980, 110962, 110970; järjestys säilyy.
- Paneeli pysyy localhostissa eikä julkaise lapsen nimeä tai tilitunnuksia statusvastauksessa.
- `--ilman-maksua --ei-jono` pysyy pakollisena.
- Testit saavat käyttää vain loopback-TestShopia.
- Muutokset PR #8:ssa; ei mergeä mainiin ilman erillistä pyyntöä.

## Review Focus
- Avoin ostoskori ei ole maksettu paikka; status pitää sanoa tämä.
- Katkeavan verkkoyhteyden aikana automaatti ei saa vahingossa varata muuta kurssia.
- Käyttäjän manuaalinen käynnistys ei saa ohittaa olemassa olevaa varausta.
- Korvaavan selainprofiilin kori ei välttämättä ole sama ostoskori.
- Avaushetken ruuhka ei saa käynnistää varauspyyntöä ennen julkaistua avausaikaa.

---

### Task 1: Paneelin häiriötilat
**Files:** `uhkone/panel.py`; `tests/test_panel.py`
**Interfaces:** `PanelState.progress(phase, metadata)`, `PanelState.public()`
- [ ] Kirjoita regressiot: retry -> ruuhka, reservation_pending -> epävarma, autentikoinnin vanhentuminen -> käyttäjän toimenpide; status ei paljasta poikkeuksen tekstiä.
- [ ] Aja testi punaiseksi CI:ssa.
- [ ] Toteuta tilaluokittelu, viimeisimmän onnistuneen tarkistuksen aikaleima ja turvallinen virheilmoitus.
- [ ] Varmenna CI vihreäksi.

### Task 2: Turvallinen manuaalinen varatie
**Files:** `uhkone/panel.py`; `tests/test_panel_browser.py`, README
**Interfaces:** `PanelState.course_info(courses)`, julkiset kurssilinkit.
- [ ] Testaa, että linkki avautuu vain hyväksytyn kurssin julkiseen kauppasivuun, ei käytä maksupyyntöjä eikä näytä väitettä yhteisestä ostoskorista.
- [ ] Varmenna epäonnistuva testi.
- [ ] Toteuta linkit ja käyttöliittymän ohje: pysäytä automaatti, tarkista olemassa oleva ostoskori saman profiilin kautta ennen uutta ostosta.
- [ ] Varmenna CI vihreäksi.

### Task 3: Simuloitu avaus- ja ruuhkanopeuden testi
**Files:** `tests/test_panel_browser.py` tai uusi `tests/test_opening_latency.py`; `uhkone/testshop.py` vain tarvittaessa.
- [ ] Lisää testi, jossa avausaika on tulevaisuudessa ja serverin alkuvastauksissa tulee 503/Retry-After; mittaa aikaleima ja varmistaa yksi tai nolla add POST, ei koskaan maksua.
- [ ] Varmenna testi ensin punaiseksi puuttuvan mittauksen vuoksi.
- [ ] Toteuta minimaalinen mittaus ja korjaa havaitut ei-turvalliset poikkeamat.
- [ ] Aja koko CI sekä selaintestit, raportoi raja-arvot ja epävarmuudet.
