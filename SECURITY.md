# Henkilötiedot ja tietoturva

Tämä on julkinen lähdekoodirepository. Älä tallenna tänne henkilötietoja, oikeiden asiakastilien tunnisteita, selaimen profiilia tai evästeitä, salasanoja, maksutietoja, raakamuotoisia verkkovastauksia, kuvakaappauksia, HAR-tiedostoja, ajonaikaisia raportteja tai lokeja. Kielto koskee myös issueita, pull requestien tekstejä, commit-viestejä ja Git-historiaa. Testeissä käytetään vain kuvitteellisia osallistujia.

## Paikallinen käyttö

- Anna osallistujan nimi mieluiten `uhkone ohjattu` -kyselyssä, jolloin se ei päädy käynnistyskomennon mukana oman päätteen komentohistoriaan.
- Kirjaudu itse näkyvässä selaimessa. Älä anna salasanaa, korttinumeroa, turvakoodia tai pankkitunnuksia komentoriviohjelmaan.
- Säilytä selainprofiili ja etenemistiedot yksityisessä hakemistossa lähdekansion ulkopuolella. Oletuspaikka on `~/.local/state/uhkone` tai `$XDG_STATE_HOME/uhkone`.
- Älä tallenna päätteen istuntoa tai lähetä koko tilahakemistoa vianetsintää varten. Käytä kuvitteellisilla henkilöillä toistettavaa paikallista testikauppaa.
- Epäselvän varauksen tai maksun tilatietoja ei poisteta uuden yrityksen tekemiseksi. Jatka samalla profiililla ja valinnoilla.

## Ennen julkaisemista

Tarkista `git diff --cached` ja suorita `python scripts/check_public_tree.py`. Tarkistin lukee koko Git-indexin sisällön ja raportoi vain tiedoston, rivin sekä löydöksen kategorian. Se ei tunnista kaikkia mahdollisia henkilötietoja, joten myös manuaalinen tarkistus on tarpeen. Paikallisen hookin saa käyttöön komennolla `git config core.hooksPath .githooks`.

Tarkista myös commitin tekijä- ja committer-tiedot ennen ensimmäistä committia. Älä julkaise henkilökohtaista nimeä tai osoitetta vahingossa Git-metadatassa. Käytä tarvittaessa GitHubin yksityisyyttä suojaavaa osoitetta ja yleistä tekijänimeä.

`uhkone/data/courses.json` on sallittu julkisen kurssidatan poikkeus. Sen kenttälista sallii vain julkisia kurssitietoja ja sulkee pois asiakas-, osallistuja- sekä ohjaajatiedot. Älä korvaa tiedostoa kirjautuneesta istunnosta tallennetulla vastauksella.

## Jos yksityistä tietoa päätyy Gitiin

Pysäytä julkaiseminen. Älä kopioi löytynyttä tietoa julkiseen issueen tai commit-viestiin. Pelkkä poistocommit ei poista tietoa historiasta.

Jos tieto ehti julkiseksi, peruuta tai vaihda mahdollisesti paljastuneet tunnukset ja kirjautumisistunnot. Selvitä ylläpitäjän kanssa historian puhdistaminen ja välimuistien tai muiden kopioiden käsittely. Tietoa voi olla jo kopioitu, vaikka alkuperäinen commit poistettaisiin.

Jos ilmoitat ohjelmavirheestä julkisesti, käytä paikallisessa testikaupassa toistettavaa esimerkkiä ja kuvitteellisia tietoja. Älä sisällytä oikean tilin sisältöä, vaikka poistaisit näkyvän nimen: varaus-, asiakas- ja kuittitunnisteet voivat myös yksilöidä käyttäjän.
