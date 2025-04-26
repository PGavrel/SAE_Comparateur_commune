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
import unicodedata
import json

# Charger les variables d'environnement depuis un fichier .env
load_dotenv()

# Config Streamlit
st.set_page_config(page_title="Comparateur de communes", layout="centered")

# Config Wikipedia
wikipedia.set_lang("fr")

# --- Configuration API Pôle Emploi / France Travail ---
token_url = "https://entreprise.pole-emploi.fr/connexion/oauth2/access_token?realm=/partenaire"
offers_url = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
client_id = "PAR_comparateurdecommunes_28abf13883e0a4d33045fd2855357075c9ae2f4181a8d90b560b74eb88f19c0c"
client_secret = "c6ca0861b738e9c7b2a282d028e436fd03bc8ee733e3ad6ace1be90aaf1eb243"
scope = "o2dsoffre api_offresdemploiv2"

# --- Authentification OAuth ---
def authenticate(scope):
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    params = {
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
        'scope': scope
    }
    response = requests.post(token_url, data=params, headers=headers)
    response.raise_for_status()
    return response.json().get('access_token')

# --- Requête “promoteur” sans pagination pour comparaison de deux villes ---
def liste_metier(code_dep, mots_cles, access_token):
    url = offers_url
    querystring = {"departement": code_dep, "motsCles": mots_cles}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    resp = requests.get(url, headers=headers, params=querystring)
    try:
        return resp.json()
    except json.JSONDecodeError:
        return {}

# --- Fonction utilitaire pour retirer les accents et formater le nom de la ville ---
def enlever_accents(texte):
    return ''.join(
        c for c in unicodedata.normalize('NFD', texte)
        if unicodedata.category(c) != 'Mn'
    ).replace(' ', '-')

# --- Requête avec pagination pour “Zoom sur une ville” ---
def get_api_headers(start, page_size):
    token = authenticate(scope)
    return {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'Range': f'bytes={start}-{start+page_size-1}'
    }

def fetch_offres(start=0, page_size=20, departement=None, mots_cles=None):
    headers = get_api_headers(start, page_size)
    params = {}
    if departement:
        params['departement'] = departement
    if mots_cles:
        params['motsCles'] = mots_cles
    resp = requests.get(offers_url, headers=headers, params=params)
    resp.raise_for_status()
    data = resp.json()
    return data.get('resultats', []), resp.headers.get('Content-Range', '')


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
def charger_dvf_aggrege(path="dvf2023.csv"):
    try:
        df = pd.read_csv(path, dtype={"INSEE_COM": str})

        # Sécuriser les colonnes si elles existent
        if "Prixm2Moyen" in df.columns:
            df["Prixm2Moyen"] = df["Prixm2Moyen"].round(1)

        if "NbApparts" in df.columns:
            df["NbApparts"] = df["NbApparts"].fillna(0).astype(int)

        if "NbMaisons" in df.columns:
            df["NbMaisons"] = df["NbMaisons"].fillna(0).astype(int)

        return df
    
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
    
       

        
    # Trier df_emploi1
    if not df_emploi1.empty and "TIME_PERIOD" in df_emploi1.columns and "PCS" in df_emploi1.columns:
        df_emploi1 = df_emploi1.sort_values(by=["TIME_PERIOD", "PCS"], ascending=[False, True])
    else:
        st.warning(f"Aucune donnée emploi disponible pour {ville1}")
    if ville2:
        if not df_emploi2.empty and "TIME_PERIOD" in df_emploi2.columns and "PCS" in df_emploi2.columns:
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
        df_dvf = charger_dvf_aggrege()

        if not df_dvf.empty:
            ligne1 = df_dvf[df_dvf["INSEE_COM"] == code1]
            ligne2 = df_dvf[df_dvf["INSEE_COM"] == code2] if ville2 else pd.DataFrame()
            prix = ligne1["Prixm2Moyen"].values[0]
            NbApparts = ligne1["NbApparts"].values[0]
            NbMaisons = ligne1["NbMaisons"].values[0]
            if ville2:
                col1, col2 = st.columns(2)
                with col1:
                    if not ligne1.empty:
                        st.markdown(f"""
                            <b style='font-size:22px'>{ville1}</b><b>
                            Prix moyen : <b>{prix} €/m²</b><b>
                            Maison : <b>{NbMaisons} €/m²</b><b>
                            Appartements : <b>{NbApparts}</b>
                            """, unsafe_allow_html=True)
                    else:
                        st.info(f"Aucune donnée DVF pour {ville1}")

                with col2:
                    if not ligne2.empty:
                        prix = ligne2["Prixm2Moyen"].values[0]
                        NbApparts = ligne2["NbApparts"].values[0]
                        NbMaisons = ligne2["NbMaisons"].values[0]
                        st.markdown(f"""
                        <b style='font-size:22px'>{ville2}</b><b>
                        Prix moyen : <b>{prix} €/m²</b><b>
                        Maison : <b>{NbMaisons} €/m²</b><b>
                        Appartements : <b>{NbApparts}</b>
                        """, unsafe_allow_html=True)
                    else:
                        st.info(f"Aucune donnée DVF pour {ville2}")
            else:
                if not ligne1.empty:
                    st.markdown(f"""
                            <b style='font-size:22px'>{ville1}</b><b>
                            Prix moyen : <b>{prix} €/m²</b><b>
                            Maison : <b>{NbMaisons} €/m²</b><b>
                            Appartements : <b>{NbApparts}</b>
                            """, unsafe_allow_html=True)
                else:
                     st.info(f"Aucune donnée DVF pour {ville1}")
        else:
            st.warning("Données non disponibles")


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
    st.title("🔎 Zoom sur une Ville")

    # Sélection de la commune
    ville = st.selectbox("Choisissez une commune", list(code_insee_villes.keys()))
    afficher_resultats_aligne(ville)  # votre bloc existant d’infos géo

    # Sidebar : filtres et activation
    if st.sidebar.checkbox("Afficher les offres d'emploi", True):
        mot_cle   = st.sidebar.text_input("🔍 Mot-clé métier", "", key="zoom_keyword")
        nb_offres = st.sidebar.selectbox("🔢 Nombre d'offres", ["5", "10", "50", "100", "Toutes"], key="zoom_nb")

        st.markdown("<hr style='border: 1px solid #ddd; margin-top: 10px;'>", unsafe_allow_html=True)

        # Préparation du slug et du code département
        slug_ville = enlever_accents(ville.split(' (')[0])
        infos = get_infos_commune(code_insee_villes[ville])
        depart = infos.get('departement', {}).get('code')

        # Appel API
        token = authenticate(scope)
        resultats = liste_metier(depart, f"{mot_cle} {slug_ville}", token).get("resultats", [])

        # Affichage vertical des offres
        if resultats:
            if nb_offres != "Toutes":
                resultats = resultats[:int(nb_offres)]
            for offre in resultats:
                st.markdown(f"### {offre.get('intitule', '—')}")
                st.write(f"📍 Lieu : {offre.get('lieuTravail', {}).get('libelle', 'Inconnu')}")
                st.write(f"📝 {offre.get('description', '')[:300]}…")
                url = (offre.get('origineOffre', {}).get('urlOrigine')
                       or offre.get('lienOrigine')
                       or offre.get('urlOrigine', "#"))
                st.markdown(f"[🔗 Voir l'offre]({url})", unsafe_allow_html=True)
                st.markdown("<hr style='border: 1px solid #ddd;'>", unsafe_allow_html=True)
        else:
            st.warning("Aucune offre trouvée pour cette ville.")


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
