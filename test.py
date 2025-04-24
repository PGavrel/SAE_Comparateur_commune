import streamlit as st
import requests
import urllib.parse
import re
import wikipedia
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
from dotenv import load_dotenv
from PIL import Image
from pathlib import Path
import pandas as pd
import streamlit.components.v1 as components
from datetime import datetime
import folium
from streamlit_folium import st_folium
import io
import zipfile
import matplotlib.pyplot as plt

# Charger les variables d'environnement depuis un fichier .env
load_dotenv()

# Config Streamlit
st.set_page_config(page_title="Comparateur de communes", layout="centered")

# Config Wikipedia
wikipedia.set_lang("fr")

# Remplacez par l'URL de l'API de France Travail et la clé API
url = "https://api.francetravail.io/partenaire/offresdemploi"  # Exemple d'URL, à adapter selon la documentation

headers = {
    "Authorization": "5cb42c37f401d792090b7141eb28396bc23836a558e2d3d7f94b2d14c0f17968",  # Remplacez par votre clé API
}

# Effectuer une requête GET pour récupérer les offres d'emploi
response = requests.get(url, headers=headers)

if response.status_code == 200:
    data = response.json()  # Si la requête est réussie, on récupère les données en format JSON
    print(data)
else:
    print(f"Erreur {response.status_code}: {response.text}")


# Communes
@st.cache_data(show_spinner="Chargement des villes...")
def charger_code_insee_villes(path="communes-france-2025.csv"):
    df = pd.read_csv(path, sep=";", dtype=str)
    # Si 'nom_standard' existe, on la renomme
    if "nom_standard" in df.columns:
        df = df.rename(columns={"nom_standard": "nom_ville"})
    return dict(zip(df["nom_ville"], df["code_insee"]))
code_insee_villes = charger_code_insee_villes()

# Fonction GeoAPI pour récupérer des infos générales sur la ville
@st.cache_data(show_spinner="Chargement des données...")
def get_infos_commune(code_insee):
    url = f"https://geo.api.gouv.fr/communes/{code_insee}"
    params = {
        "fields": "nom,code,codeDepartement,codeRegion,codesPostaux,centre,surface,population,departement,region"
    }
    response = requests.get(url, params=params)
    if response.status_code == 200:
        return response.json()
    else:
        return {}


# Fonction Melodi (INSEE) information emploi
@st.cache_data(show_spinner="Chargement des données emploi INSEE...")
def get_emploi_melodi_insee(dep_code):
    url = f"https://api.insee.fr/melodi/data/DS_RP_EMPLOI_LR_COMP?GEO={dep_code}"

    try:
        response = requests.get(url, verify=False)
        if response.status_code != 200 or not response.text.strip():
            st.warning(f"Réponse vide ou erreur HTTP {response.status_code} pour {dep_code}")
            return pd.DataFrame()

        data = response.json()

        observations = data.get('observations', [])
        if not observations:
            st.warning(f"Aucune donnée d'observation disponible pour {dep_code}")
            return pd.DataFrame()

        extracted_data = []
        for obs in observations:
            dimensions = obs.get('dimensions', {})
            attributes = obs.get('attributes', {})
            measures = obs.get('measures', {}).get('OBS_VALUE_NIVEAU', {}).get('value', None)

            combined_data = {**dimensions, **attributes, 'OBS_VALUE_NIVEAU': measures}
            extracted_data.append(combined_data)

        return pd.DataFrame(extracted_data)

    except Exception as e:
        st.error(f"Erreur API Melodi : {e}")
        return pd.DataFrame()
   
PCS_LABELS = {
    "1": "Agriculteurs exploitants",
    "2": "Artisans, commerçants, chefs d'entreprise",
    "3": "Cadres et professions intellectuelles",
    "4": "Professions intermédiaires",
    "5": "Employés",
    "6": "Ouvriers",
    "7": "Retraités",
    "8": "Autres inactifs",
    "9": "Non renseigné",
    "_T": "Total"
}
def ajouter_libelles_pcs(df):
    if "PCS" in df.columns:
        df = df.copy()
        df["PCS_LIBELLE"] = df["PCS"].astype(str).map(PCS_LABELS).fillna("Inconnu")
    return df
def regrouper_emploi(df):
    if "TIME_PERIOD" in df.columns and "PCS" in df.columns and "OBS_VALUE_NIVEAU" in df.columns:
        df_grouped = (
            df.groupby(["TIME_PERIOD", "PCS"], as_index=False)
              .agg({"OBS_VALUE_NIVEAU": "sum"})
              .round({"OBS_VALUE_NIVEAU": 0})
        )
        return df_grouped
    return df

@st.cache_data(show_spinner="Chargement des données DVF...")
def get_prix_m2_dvf(code_insee):
    url = f"https://datafoncier.cquest.org/dvf/latest/csv/{code_insee}.csv"

    try:
        response = requests.get(url, verify=False)
        if response.status_code != 200:
            return None

        df = pd.read_csv(io.StringIO(response.text), sep="|")

        # Nettoyage
        df = df[df["Type local"].isin(["Maison", "Appartement"])]
        df = df[df["Surface reelle bati"] > 15]
        df = df[df["Valeur fonciere"] > 10000]

        df["prix_m2"] = df["Valeur fonciere"] / df["Surface reelle bati"]
        prix_m2_moyen = df["prix_m2"].mean().round(1)

        return prix_m2_moyen

    except Exception as e:
        st.error(f"Erreur récupération DVF : {e}")
        return None


# Fonction GeoAPI pour récupérer le nom officiel depuis le code INSEE
@st.cache_data(show_spinner="Chargement des données...")
def get_nom_officiel_depuis_insee(code_insee):
    infos = get_infos_commune(code_insee)
    return infos.get("nom")

# Récupérer blason et site web via API Wikipédia
@st.cache_data(show_spinner="Chargement des données...")
def get_blason_et_site_via_api(nom_ville):
    if not nom_ville or not isinstance(nom_ville, str):
        return None

    # Fallback pour Paris (blason + site web)
    if nom_ville.lower() == "paris":
        return {
            "ville": "Paris",
            "blason_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/cd/Blason_Paris_final.png/800px-Blason_Paris_final.png",
            "site_web": "https://www.paris.fr",
            "image_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4b/La_Tour_Eiffel_vue_de_la_Tour_Saint-Jacques%2C_Paris_ao%C3%BBt_2014_%282%29.jpg/1920px-La_Tour_Eiffel_vue_de_la_Tour_Saint-Jacques%2C_Paris_ao%C3%BBt_2014_%282%29.jpg"
        }

    try:
        page_title = wikipedia.search(nom_ville)[0]
    except (IndexError, wikipedia.exceptions.WikipediaException):
        return None

    ville_url = urllib.parse.quote(page_title.replace(" ", "_"))
    api_url = "https://fr.wikipedia.org/w/api.php"

    params = {
        "action": "query",
        "prop": "revisions|pageimages",
        "rvprop": "content",
        "rvslots": "main",
        "piprop": "original",
        "format": "json",
        "titles": page_title
    }

    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(api_url, params=params, headers=headers)
    data = response.json()

    pages = data["query"]["pages"]
    page = next(iter(pages.values()))

    if "revisions" not in page:
        return None

    content = page["revisions"][0]["slots"]["main"]["*"]

    blason_match = re.search(r"\|\s*blason\s*=\s*(.+)", content)
    blason = blason_match.group(1).strip() if blason_match else None

    site_match = re.search(r"\|\s*siteweb\s*=\s*(.+)", content)
    siteweb = site_match.group(1).strip() if site_match else None

    if blason and not blason.startswith("http"):
        blason_filename = blason.replace(" ", "_")
        blason_url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{urllib.parse.quote(blason_filename)}"
    else:
        blason_url = blason

    image_url = page.get("original", {}).get("source")

    return {
        "ville": nom_ville,
        "blason_url": blason_url,
        "site_web": siteweb,
        "image_url": image_url
    }

# Carte
def get_geojson_commune(code_insee):
    url = f"https://geo.api.gouv.fr/communes/{code_insee}"
    params = {"format": "geojson", "geometry": "contour"}
    response = requests.get(url, params=params)
    if response.status_code == 200:
        return response.json()
    else:
        return None

def afficher_carte_commune_individuelle(code_insee, couleur="#3186cc"):
    geo = get_geojson_commune(code_insee)
    nom = get_nom_officiel_depuis_insee(code_insee)
    
    if geo:
        # Approximation du centre à partir des coordonnées du centre INSEE
        infos = get_infos_commune(code_insee)
        coords = infos.get("centre", {}).get("coordinates", [2.5, 46.6])  # [lon, lat]
        lat, lon = coords[1], coords[0]

        m = folium.Map(location=[lat, lon], zoom_start=11)

        folium.GeoJson(
            geo,
            name=nom,
            tooltip=nom,
            style_function=lambda x: {
                "fillColor": couleur,
                "color": couleur,
                "weight": 2,
                "fillOpacity": 0.3
            }
        ).add_to(m)

        return m
    else:
        return None



@st.cache_data
def get_temperature_par_saison_ville(lat, lon):
    current_year = datetime.now().year
    years = [current_year - 2, current_year - 1]
    
    all_data = []

    for year in years:
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": f"{year}-01-01",
            "end_date": f"{year}-12-31",
            "daily": "temperature_2m_mean",
            "timezone": "Europe/Paris"
        }

        response = requests.get(url, params=params)
        data = response.json()

    if "daily" not in data:
        return None

    df = pd.DataFrame(data["daily"])
    df["date"] = pd.to_datetime(df["time"])
    df["mois"] = df["date"].dt.month

    def saison(mois):
        if mois in [12, 1, 2]:
            return "Hiver"
        elif mois in [3, 4, 5]:
            return "Printemps"
        elif mois in [6, 7, 8]:
            return "Été"
        else:
            return "Automne"

    df["saison"] = df["mois"].apply(saison)
    moyennes_saison = df.groupby("saison")["temperature_2m_mean"].mean().round(1).to_dict()
    moyenne_annuelle = df["temperature_2m_mean"].mean().round(1)

    return moyennes_saison, moyenne_annuelle

@st.cache_data
def get_prevision_meteo(lat, lon):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": [
            "temperature_2m_max", "temperature_2m_min", "sunshine_duration",
            "sunrise", "sunset", "daylight_duration", "uv_index_max",
            "uv_index_clear_sky_max", "precipitation_sum",
            "precipitation_probability_max", "wind_speed_10m_max",
            "wind_direction_10m_dominant"
        ],
        "timezone": "auto"
    }

    response = requests.get(url, params=params)
    data = response.json()

    # Transformer les données en DataFrame
    df = pd.DataFrame(data["daily"])
    df["date"] = pd.to_datetime(data["daily"]["time"])
    return df

#afficher icone météo
def get_icone_meteo(row):
    soleil = row.get("sunshine_duration", 0)
    pluie = row.get("precipitation_sum", 0)
    risque_pluie = row.get("precipitation_probability_max", 0)

    # Cas de pluie significative
    if pluie > 10 or risque_pluie > 80:
        return "🌧️", "Risque de fortes averses"
    elif pluie > 2 or risque_pluie > 50:
        return "🌦️", "Pluie probable"
    elif pluie > 0 or risque_pluie > 30:
        return "🌂", "Possibles averses"
    elif soleil > 300:
        return "☀️", "Ensoleillé"
    elif soleil > 120:
        return "🌤️", "Partiellement ensoleillé"
    elif soleil > 30:
        return "🌥️", "Peu de soleil"
    else:
        return "☁️", "Couvert"
    
#Affichage prévision météo 
def afficher_previsions_meteo(ville, df, nb_jours):
    st.markdown(f"#### {ville}")

    if df.empty:
        st.info("Prévisions indisponibles")
        return

    jours = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]

    st.markdown("""
    <style>
        .prevision {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 16px;
            padding: 8px 0;
            border-bottom: 1px solid #eee;
        }
        .date { width: 80px; }
        .temp { width: 100px; font-weight: bold; font-size: 18px; }
        .icone { width: 60px; text-align: center; }
        .pluie { width: 60px; text-align: right; font-weight: bold; }
    </style>
    """, unsafe_allow_html=True)

    for i in range(min(nb_jours, len(df))):
        row = df.iloc[i]
        date_obj = row["date"]
        jour = jours[date_obj.weekday()]
        date_txt = f"{jour} {date_obj.day}"

        temp_max = f"{int(round(row['temperature_2m_max']))}°"
        temp_min = f"{int(round(row['temperature_2m_min']))}°"
        pluie = f"{int(round(row['precipitation_probability_max']))}%" if "precipitation_probability_max" in row else "–"

        icone, description = get_icone_meteo(row)

        st.markdown(f"""
        <div class="prevision">
            <div class="date">{date_txt}</div>
            <div class="temp">{temp_max} / {temp_min}</div>
            <div class="icone">
                <span>{icone}</span> <span style="font-size: 13px;">{description}</span>
            </div>
            <div class="pluie">
                <span>💧</span> <span style="font-size: 13px;">{pluie}</span>
            </div>
        </div>

        <style>
            .prevision {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                font-size: 16px;
                padding: 8px 6px;
                border-bottom: 1px solid #444;
            }}
            .date {{
                width: 90px;
                font-weight: 500;
            }}
            .temp {{
                width: 90px;
                display: flex;
                align-items: center;
                font-weight: bold;
                font-size: 15px;
            }}
            .icone, .pluie {{
                display: flex;
                align-items: center;
                gap: 6px;
                width: 150px;
                font-size: 14px;
            }}
        </style>
        """, unsafe_allow_html=True)

@st.cache_data
def charger_dvf_aggrege(path="dvf_agg_communes_2024.csv"):
    try:
        return pd.read_csv(path, dtype={"Code commune": str})
    except Exception as e:
        st.error(f"Erreur lecture fichier DVF agrégé : {e}")
        return pd.DataFrame()

# Afficher infos villes et côtes à côte si 2 villes 
def afficher_resultats_aligne(ville1, ville2=None):
    code1 = code_insee_villes.get(ville1)
    code2 = code_insee_villes.get(ville2) if ville2 else None

    data1 = get_blason_et_site_via_api(get_nom_officiel_depuis_insee(code1)) if code1 else None
    data2 = get_blason_et_site_via_api(get_nom_officiel_depuis_insee(code2)) if code2 else None

    infos1 = get_infos_commune(code1) if code1 else {}
    infos2 = get_infos_commune(code2) if code2 else {}

    df_test = charger_code_insee_villes()
    

    if ville2:
        st.header(f"{ville1} VS {ville2}")
    else:
        st.header(f"{ville1}")

    if st.sidebar.checkbox("Informations générales", True):
        for label, key in [
            ("Image représentative", "image_url"),
            ("Blason", "blason_url"),
            ("Site officiel", "site_web")
        ]:
            st.markdown(f"### {label}")
            if ville2:
                col1, col2 = st.columns(2)

                with col1:
                    if data1 and data1.get(key):
                        if key.endswith("url"):
                            st.image(data1[key], caption=f"{label} de {ville1}", use_container_width=True)
                        else:
                            st.markdown(f"[{ville1}]({data1[key]})")
                    else:
                        st.info(f"{label} indisponible pour {ville1}")

                with col2:
                    if data2 and data2.get(key):
                        if key.endswith("url"):
                            st.image(data2[key], caption=f"{label} de {ville2}", use_container_width=True)
                        else:
                            st.markdown(f"[{ville2}]({data2[key]})")
                    else:
                        st.info(f"{label} indisponible pour {ville2}")
            else:
                if data1 and data1.get(key):
                    if key.endswith("url"):
                        st.image(data1[key], caption=f"{label} de {ville1}", use_container_width=True)
                    else:
                        st.markdown(f"[{ville1}]({data1[key]})")
                else:
                    st.info(f"{label} indisponible pour {ville1}")

        st.markdown("### Informations générales")
        for label, key in [
            ("Population", "population"),
            ("Superficie (km²)", "surface"),
            ("Code postal", "codesPostaux"),
            ("Département", "departement"),
            ("Région", "region")
        ]:
            if ville2:
                col1, col2 = st.columns(2)

                with col1:
                    val = infos1.get(key)
                    if isinstance(val, dict):
                        val = val.get("nom")
                    elif isinstance(val, list):
                        val = ", ".join(val)
                    st.markdown(f"**{label} :** {val if val else 'Non disponible'}")

                with col2:
                    val = infos2.get(key)
                    if isinstance(val, dict):
                        val = val.get("nom")
                    elif isinstance(val, list):
                        val = ", ".join(val)
                    st.markdown(f"**{label} :** {val if val else 'Non disponible'}")
            else:
                val = infos1.get(key)
                if isinstance(val, dict):
                    val = val.get("nom")
                elif isinstance(val, list):
                    val = ", ".join(val)
                st.markdown(f"**{label} :** {val if val else 'Non disponible'}")



    if not df_emploi1.empty and "TIME_PERIOD" in df_emploi1.columns and "PCS" in df_emploi1.columns:
        # Section emploi via INSEE Melodi
        if st.sidebar.checkbox("Répartition emploi", True):
            st.markdown("### Répartition catégories socio-professionnel département(source : INSEE - API Melodi)")
            dep1 = infos1.get("departement", {}).get("code")
            dep2 = infos2.get("departement", {}).get("code") if ville2 else None
    
            code_dep1 = f"DEP-{dep1}" if dep1 else None
            code_dep2 = f"DEP-{dep2}" if ville2 and dep2 else None
    
            df_emploi1 = get_emploi_melodi_insee(code_dep1) if code_dep1 else pd.DataFrame()
            df_emploi2 = get_emploi_melodi_insee(code_dep2) if code_dep2 else pd.DataFrame()
            df_emploi1 = regrouper_emploi(df_emploi1)
            df_emploi2 = regrouper_emploi(df_emploi2)
            df_emploi1 = ajouter_libelles_pcs(df_emploi1)
            df_emploi2 = ajouter_libelles_pcs(df_emploi2)
    
            # Affichage simplifié (par sexe et PCS par exemple)
            colonnes = ["TIME_PERIOD","PCS", "PCS_LIBELLE", "OBS_VALUE_NIVEAU"]
    
            # Création du graphique d'évolution temporelle
            fig, ax = plt.subplots(figsize=(10, 6))
            
            for pcs in df_emploi1["PCS_LIBELLE"].unique():
                data1 = df_emploi1[df_emploi1["PCS_LIBELLE"] == pcs]
                data2 = df_emploi2[df_emploi2["PCS_LIBELLE"] == pcs]
                ax.plot(data1["TIME_PERIOD"], data1["OBS_VALUE_NIVEAU"], marker='o', label=f"{pcs} - Ville 1")
                ax.plot(data2["TIME_PERIOD"], data2["OBS_VALUE_NIVEAU"], marker='x', linestyle='--', label=f"{pcs} - Ville 2")
            
            ax.set_title("Évolution des catégories socio-professionnelles par département")
            ax.set_xlabel("Année")
            ax.set_ylabel("Population active")
            ax.legend()
            plt.xticks(rotation=45)
            plt.tight_layout()
            
            plt.show()
        else:
            st.warning(f"Aucune donnée emploi disponible pour {ville1}")
        
        # Trier df_emploi1
        if not df_emploi1.empty and "TIME_PERIOD" in df_emploi1.columns and "PCS" in df_emploi1.columns:
            df_emploi1 = df_emploi1.sort_values(by=["TIME_PERIOD", "PCS"], ascending=[False, True])
        else:
            st.warning(f"Aucune donnée emploi disponible pour {ville1}")
        if ville2:
            if not df_emploi1.empty and "TIME_PERIOD" in df_emploi1.columns and "PCS" in df_emploi1.columns:
                df_emploi2 = df_emploi2.sort_values(by=["TIME_PERIOD", "PCS"], ascending=[False, True])
            else:
                st.warning(f"Aucune donnée emploi disponible pour {ville1}")

        # Trier df_emploi2
        if ville2:
            colonnes_tri2 = [col for col in ["TIME_PERIOD", "PCS"] if col in df_emploi2.columns]
            if colonnes_tri2:
                df_emploi2 = df_emploi2.sort_values(by=colonnes_tri2, ascending=[False, True])

        if ville2:
            st.write(f"### {ville1}")
            st.dataframe(df_emploi1[ [col for col in colonnes if col in df_emploi1.columns] ])
            st.write(f"### {ville2}")
            st.dataframe(df_emploi2[ [col for col in colonnes if col in df_emploi2.columns] ])
        else:
            st.dataframe(df_emploi1[ [col for col in colonnes if col in df_emploi1.columns] ])


    # Récupération coordonnées GPS depuis infos INSEE (attention à l'ordre GeoJSON)
    coords1 = infos1.get("centre", {}).get("coordinates")
    lat1, lon1 = (coords1[1], coords1[0]) if coords1 else (None, None)

    coords2 = infos2.get("centre", {}).get("coordinates") if ville2 else None
    lat2, lon2 = (coords2[1], coords2[0]) if coords2 else (None, None)
    
    # Carte
    if st.sidebar.checkbox("Carte", True):
        st.markdown("### Cartes")

        if ville2:
            col1, col2 = st.columns(2)

            with col1:
                m1 = afficher_carte_commune_individuelle(code1, couleur="#3186cc")
                if m1:
                    st_folium(m1, height=400)
                else:
                    st.warning(f"Carte indisponible pour {ville1}")

            with col2:
                m2 = afficher_carte_commune_individuelle(code2, couleur="#cc3333")
                if m2:
                    st_folium(m2, height=400)
                else:
                    st.warning(f"Carte indisponible pour {ville2}")
        else:
            m1 = afficher_carte_commune_individuelle(code1, couleur="#3186cc")
            if m1:
                st_folium(m1, height=400)
            else:
                st.warning(f"Carte indisponible pour {ville1}")



    # Températures Open-Meteo
    if st.sidebar.checkbox("Météo", True):
        st.markdown("### Température moyenne des 2 dernières années (source : Open-Meteo)")
        
        saisons1, moyenne1 = get_temperature_par_saison_ville(lat1, lon1) if lat1 and lon1 else ({}, None)
        saisons2, moyenne2 = get_temperature_par_saison_ville(lat2, lon2) if ville2 and lat2 and lon2 else ({}, None)
    
        if ville2:
            col1, col2 = st.columns(2)

            with col1:
                if moyenne1:
                    st.markdown(f" Température annuelle moyenne : {moyenne1} °C")
                for saison in ["Hiver", "Printemps", "Été", "Automne"]:
                    st.markdown(f"- {saison} : {saisons1.get(saison, 'N/A')} °C")
                
            with col2:
                if moyenne2:
                    st.markdown(f" Température annuelle moyenne : {moyenne2} °C")
                for saison in ["Hiver", "Printemps", "Été", "Automne"]:
                    st.markdown(f"- {saison} : {saisons2.get(saison, 'N/A')} °C")
                

        else:
            if moyenne1:
                st.markdown(f" Température annuelle moyenne : {moyenne1} °C")
            for saison in ["Hiver", "Printemps", "Été", "Automne"]:
                st.markdown(f"- {saison} : {saisons1.get(saison, 'N/A')} °C")
            


        # prévisions Open-Meteo
        st.markdown(f"## Prévisions météo (source : Open-Meteo)")

        # Récupération données depuis API Open-Meteo
        prevision1 = get_prevision_meteo(lat1, lon1) if lat1 and lon1 else pd.DataFrame()
        prevision2 = get_prevision_meteo(lat2, lon2) if ville2 and lat2 and lon2 else pd.DataFrame()
        # Nombre de jours à afficher (ex: 5 prochains jours)
        nb_jours = 5

        # Affichage en colonnes ou seul
        if ville2:
            col1, col2 = st.columns(2)

            with col1:
                afficher_previsions_meteo(ville1, prevision1, nb_jours)

            with col2:
                afficher_previsions_meteo(ville2, prevision2, nb_jours)
        
        else:
            afficher_previsions_meteo(ville1, prevision1, nb_jours)


    if st.sidebar.checkbox("Logement", True):
        st.markdown("### Prix moyen au m² (transactions DVF – Etalab)")
        df_dvf_agg = charger_dvf_aggrege()

        if not df_dvf_agg.empty:
            ligne1 = df_dvf_agg[df_dvf_agg["Code commune"] == code1]
            ligne2 = df_dvf_agg[df_dvf_agg["Code commune"] == code2] if ville2 else pd.DataFrame()

            if ville2:
                col1, col2 = st.columns(2)
                with col1:
                    if not ligne1.empty:
                        prix = ligne1["prix_m2_moyen"].values[0]
                        nb = ligne1["nb_ventes"].values[0]
                        st.success(f"**{ville1}** : {prix} €/m² ({nb} ventes)")
                    else:
                        st.info(f"Aucune donnée DVF pour {ville1}")

                with col2:
                    if not ligne2.empty:
                        prix = ligne2["prix_m2_moyen"].values[0]
                        nb = ligne2["nb_ventes"].values[0]
                        st.success(f"**{ville2}** : {prix} €/m² ({nb} ventes)")
                    else:
                        st.info(f"Aucune donnée DVF pour {ville2}")
            else:
                if not ligne1.empty:
                    prix = ligne1["prix_m2_moyen"].values[0]
                    nb = ligne1["nb_ventes"].values[0]
                    st.success(f"**{ville1}** : {prix} €/m² ({nb} ventes)")
                else:
                    st.info(f"Aucune donnée DVF pour {ville1}")
        else:
            st.warning("Fichier `dvf_agg_communes_2024.csv` introuvable ou vide.")


# Fonction pour envoyer un email
def envoyer_email(idee):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    receiver_email = os.getenv("RECEIVER_EMAIL")

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = "Nouvelle idée soumise"

    body = f"Une nouvelle idée a été soumise :\n\n{idee}"
    msg.attach(MIMEText(body, 'plain'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
        st.success("Votre idée a été soumise avec succès !")
    except Exception as e:
        st.error(f"Erreur lors de l'envoi de l'email : {e}")

# Page d'accueil
def page_accueil():
    st.title("Bienvenue sur le Comparateur de communes françaises !")
    st.write("Cette application vous permet de découvrir facilement des informations sur les communes françaises. " \
    "Vous pouvez aussi comparer deux villes côte à côte, pour mieux visualiser leurs différence. ")
    st.markdown("Ce comparateur a été imaginé et développé par Tristan Coadou et Pierre Gavrel.")
    
    components.html("""
    <div id="linkedin-badges" style="display: flex; gap: 30px; flex-wrap: wrap; justify-content: center;"></div>

    <script>
        const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        const theme = prefersDark ? "dark" : "light";

        const badgesHtml = `
            <!-- Badge 1 -->
            <div style="width: 320px; height: 200px;">
                <div class="badge-base LI-profile-badge"
                    data-locale="fr_FR"
                    data-size="large"
                    data-theme="${theme}"
                    data-type="VERTICAL"
                    data-vanity="tristan-coadou-95484724b"
                    data-version="v1">
                    <a class="badge-base__link LI-simple-link"
                    href="https://www.linkedin.com/in/tristan-coadou-95484724b?trk=profile-badge">
                    </a>
                </div>
            </div>

            <!-- Badge 2 -->
            <div style="width: 320px; height: 200px;">
                <div class="badge-base LI-profile-badge"
                    data-locale="fr_FR"
                    data-size="large"
                    data-theme="${theme}"
                    data-type="VERTICAL"
                    data-vanity="pierregavrel"
                    data-version="v1">
                    <a class="badge-base__link LI-simple-link"
                    href="https://fr.linkedin.com/in/pierregavrel?trk=profile-badge">
                    </a>
                </div>
            </div>
        `;

        document.getElementById("linkedin-badges").innerHTML = badgesHtml;
        const script = document.createElement("script");
        script.src = "https://platform.linkedin.com/badges/js/profile.js";
        script.async = true;
        script.defer = true;
        document.body.appendChild(script);
    </script>
    """, height=400)
    
# Page City Fighter
def page_city_fighter():
    st.title("City Fighter")
    villes_disponibles = list(code_insee_villes.keys())

    col1, col2 = st.columns(2)
    with col1:
        ville1 = st.selectbox("Choisissez la première commune", villes_disponibles, index=0, key="ville1")
    with col2:
        ville2 = st.selectbox("Choisissez la seconde commune", [ville for ville in villes_disponibles if ville != st.session_state.ville1], index=0, key="ville2")

    afficher_resultats_aligne(ville1, ville2)

# Page Zoom Ville
def page_zoom_ville():
    st.title("Zoom sur une Ville")
    villes = list(code_insee_villes.keys())
    default_index = villes.index("Toulouse") if "Toulouse" in villes else 0
    ville = st.selectbox("Choisissez une commune", villes, index=default_index)
    afficher_resultats_aligne(ville)


# Page Boîte à Idées
def page_boite_a_idees():
    st.title("Boîte à Idées")
    st.write("Proposez vos idées pour améliorer l'application !")
    email_utilisateur = st.text_input("Votre adresse email :")
    idee = st.text_area("Votre idée :")
    if st.button("Soumettre"):
        if idee.strip() and email_utilisateur.strip():
            envoyer_email(idee, email_utilisateur)
        else:
            st.warning("Veuillez entrer une idée et votre adresse email avant de soumettre.")

# Fonction pour envoyer un email avec l'idée et l'email de l'utilisateur
def envoyer_email(idee, email_utilisateur):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    receiver_email = os.getenv("RECEIVER_EMAIL")

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = "Nouvelle idée soumise"

    body = f"Une nouvelle idée a été soumise par {email_utilisateur} :\n\n{idee}"
    msg.attach(MIMEText(body, 'plain'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
        st.success("Votre idée a été soumise avec succès !")
    except Exception as e:
        st.error(f"Erreur lors de l'envoi de l'email : {e}")

# Logo ou illustration si tu veux
#st.sidebar.image("https://cdn-icons-png.flaticon.com/512/684/684908.png", width=100)


# Titre principal
st.sidebar.markdown("## 🧭 Navigation")

# Navigation avec icônes
page = st.sidebar.radio(
    "Choisis une page 👇",
    [
        "🏠 Accueil",
        "⚔️ City Fighter",
        "🔍 Zoom Ville",
        "💡 Boîte à Idées"
    ],
    index=0,
    label_visibility="collapsed"
)

# Pour adapter la page selon la sélection :
page = page.split(" ", 1)[1]  # Extrait le nom réel sans l'emoji (facultatif)

# Ligne de séparation pour City Fighter et Zoom Ville
if page == "City Fighter" or page == "Zoom Ville":
    st.sidebar.markdown("---")



# Affichage de la page sélectionnée
if page == "Accueil":
    page_accueil()
elif page == "City Fighter":
    page_city_fighter()
elif page == "Zoom Ville":
    page_zoom_ville()
elif page == "Boîte à Idées":
    page_boite_a_idees()

