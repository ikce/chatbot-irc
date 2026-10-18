"""
title: Web Fetch + Vision
description: Lit les pages web (texte ET images) et les liens directs vers des images. Les images sont analysées par un modèle Grok avec vision.
version: 2.0.1
"""

import asyncio
import base64
import html as htmllib
import io
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import aiohttp
from pydantic import BaseModel, Field

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from PIL import Image
except ImportError:
    Image = None

SKIP_IMG = re.compile(
    r"(icon|logo|sprite|avatar|pixel|emoji|badge|spinner|blank|placeholder|tracking)",
    re.I,
)


# Accept pour les images : certains hébergeurs (Imgur…) redirigent vers une
# page HTML/JavaScript quand la requête annonce préférer text/html.
IMAGE_ACCEPT = "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5"
IMG_EXT = re.compile(r"\.(png|jpe?g|gif|webp|avif|bmp)$", re.I)


class Tools:
    class Valves(BaseModel):
        xai_api_key: str = Field(
            default="", description="Clé API xAI (nécessaire pour analyser les images)"
        )
        xai_base_url: str = Field(default="https://api.x.ai/v1")
        vision_model: str = Field(
            default="grok-4.7",
            description="ID du modèle Grok avec vision (le même ID que dans ta liste de modèles)",
        )
        describe_images: bool = Field(
            default=True, description="Analyser les images des pages web"
        )
        max_images: int = Field(
            default=4, description="Nombre max d'images analysées par page"
        )
        min_image_px: int = Field(
            default=150, description="Ignore les images plus petites (icônes, pubs, pixels)"
        )
        max_chars: int = Field(
            default=12000, description="Nombre max de caractères de texte retournés"
        )
        timeout: int = Field(default=15, description="Timeout réseau en secondes")
        max_redirects: int = Field(default=5)
        block_private_ips: bool = Field(
            default=True,
            description="Bloque localhost / LAN / adresses internes (protège le homelab)",
        )
        user_agent: str = Field(
            default="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
        )

    def __init__(self):
        self.valves = self.Valves()

    # ---------- utilitaires ----------

    async def _status(self, emitter, msg, done=False):
        if emitter:
            await emitter({"type": "status", "data": {"description": msg, "done": done}})

    def _vision_ready(self):
        return self.valves.describe_images and bool(self.valves.xai_api_key.strip())

    async def _is_safe(self, url: str):
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False, "URL invalide (http/https seulement)"
        if not self.valves.block_private_ips:
            return True, ""
        port = p.port or (443 if p.scheme == "https" else 80)
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, port)
        except socket.gaierror:
            return False, f"impossible de résoudre {p.hostname}"
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if not ip.is_global:
                return False, f"adresse interne bloquée ({ip})"
        return True, ""

    async def _get(self, s, url, max_bytes, accept=None):
        """GET avec vérification de sécurité à chaque redirection."""
        for _ in range(self.valves.max_redirects + 1):
            ok, why = await self._is_safe(url)
            if not ok:
                raise ValueError(why)
            hdrs = {"Accept": accept} if accept else None
            async with s.get(url, allow_redirects=False, headers=hdrs) as r:
                if r.status in (301, 302, 303, 307, 308) and "Location" in r.headers:
                    url = urljoin(url, r.headers["Location"])
                    continue
                if r.status >= 400:
                    raise ValueError(f"HTTP {r.status}")
                chunks, total = [], 0
                async for chunk in r.content.iter_chunked(65536):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break
                body = b"".join(chunks)[:max_bytes]
                return url, r.headers.get("Content-Type", "").lower(), body, r.charset
        raise ValueError("trop de redirections")

    # ---------- extraction HTML ----------

    def _extract(self, raw: str, base: str):
        title, desc, imgs = "", "", []
        if BeautifulSoup:
            soup = BeautifulSoup(raw, "html.parser")
            if soup.title:
                title = soup.title.get_text(strip=True)
            meta = soup.find("meta", attrs={"property": "og:description"}) or soup.find(
                "meta", attrs={"name": "description"}
            )
            if meta and meta.get("content"):
                desc = meta["content"].strip()
            og = soup.find("meta", attrs={"property": "og:image"}) or soup.find(
                "meta", attrs={"name": "twitter:image"}
            )
            if og and og.get("content"):
                imgs.append(og["content"])
            for t in soup(
                ["script", "style", "noscript", "nav", "footer", "header",
                 "aside", "form", "svg", "iframe"]
            ):
                t.decompose()
            main = soup.find("article") or soup.find("main") or soup.body or soup
            for img in main.find_all("img"):
                src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                if (not src or src.startswith("data:")) and img.get("srcset"):
                    src = img["srcset"].split(",")[0].split()[0]
                if src:
                    imgs.append(src)
            text = main.get_text("\n", strip=True)
        else:
            m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
            title = htmllib.unescape(m.group(1).strip()) if m else ""
            imgs = re.findall(r"<img[^>]+src=[\"']([^\"']+)", raw, re.I)
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
            text = htmllib.unescape(re.sub(r"<[^>]+>", " ", text))

        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        clean = []
        for u in imgs:
            u = urljoin(base, htmllib.unescape(u.strip()))
            p = urlparse(u)
            if (
                p.scheme not in ("http", "https")
                or p.path.lower().endswith(".svg")
                or SKIP_IMG.search(p.path)
                or u in clean
            ):
                continue
            clean.append(u)
        return title, desc, text, clean

    # ---------- images / vision ----------

    def _prep_image(self, body, ctype, check_size=True):
        """Convertit en JPEG redimensionné (formats sûrs pour l'API), en base64."""
        if Image is None:
            return f"data:{ctype.split(';')[0]};base64," + base64.b64encode(body).decode()
        try:
            img = Image.open(io.BytesIO(body))
            if check_size and min(img.size) < self.valves.min_image_px:
                return None
            img = img.convert("RGB")
            img.thumbnail((1280, 1280))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None

    async def _describe(self, images, context):
        prompt = (
            f"Voici {len(images)} image(s) provenant de {context}. "
            "Pour chaque image, sous son numéro [1], [2]…, décris précisément ce qu'on voit : "
            "sujet, personnes, objets, scène, ambiance. Transcris tout texte visible "
            "(mèmes, captures d'écran, graphiques, légendes). Sois factuel et concis."
        )
        content = [{"type": "text", "text": prompt}]
        for i, (u, data_url) in enumerate(images, 1):
            content.append({"type": "text", "text": f"[{i}] {u}"})
            content.append(
                {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}}
            )
        payload = {
            "model": self.valves.vision_model,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "Authorization": f"Bearer {self.valves.xai_api_key.strip()}",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as s:
                async with s.post(
                    f"{self.valves.xai_base_url.rstrip('/')}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as r:
                    data = await r.json(content_type=None)
                    if r.status >= 400:
                        return f"(Erreur vision HTTP {r.status} : {str(data)[:300]})"
                    return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return f"(Erreur vision : {e})"

    # ---------- outil exposé au modèle ----------

    async def fetch_url(self, url: str, __event_emitter__=None) -> str:
        """
        Lit une page web ou une image en ligne. Utilise cet outil chaque fois qu'un message
        contient un lien http:// ou https:// (page, article, image, mème, capture d'écran)
        et que tu dois savoir ce qu'il contient. Retourne le texte de la page ainsi
        qu'une description détaillée des images qui s'y trouvent.
        :param url: L'URL complète à lire (ex: https://example.com/article ou https://i.imgur.com/abc.jpg)
        :return: Le titre, le texte et la description des images
        """
        url = url.strip().strip("<>\"'")
        await self._status(__event_emitter__, f"Lecture de {url}…")
        headers = {
            "User-Agent": self.valves.user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json,image/*;q=0.9,*/*;q=0.5",
            "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
        }
        timeout = aiohttp.ClientTimeout(total=self.valves.timeout)

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                accept = IMAGE_ACCEPT if IMG_EXT.search(urlparse(url).path) else None
                final, ctype, body, charset = await self._get(s, url, 10_000_000, accept)

                # --- Lien direct vers une image ---
                if ctype.startswith("image/"):
                    if not self._vision_ready():
                        return (
                            f"{final} est une image ({ctype}), mais l'analyse d'image "
                            "n'est pas configurée (valve xai_api_key vide)."
                        )
                    data_url = self._prep_image(body, ctype, check_size=False)
                    if not data_url:
                        return f"{final} est une image, mais elle n'a pas pu être décodée."
                    await self._status(__event_emitter__, "Analyse de l'image…")
                    desc = await self._describe([(final, data_url)], "un lien envoyé dans le chat")
                    await self._status(__event_emitter__, "Image analysée", True)
                    return f"URL : {final}\nType : image\n\nDescription de l'image :\n{desc}"

                # --- Page web / texte ---
                raw = body.decode(charset or "utf-8", errors="replace")
                imgs = []
                if "html" in ctype or (not ctype and "<html" in raw[:2000].lower()):
                    title, desc, text, imgs = self._extract(raw, final)
                elif any(t in ctype for t in ("text/", "json", "xml")):
                    title, desc, text = "", "", raw
                else:
                    await self._status(__event_emitter__, f"Type non supporté ({ctype})", True)
                    return f"Le lien {final} pointe vers un fichier de type {ctype}, pas une page lisible."

                image_section = ""
                if imgs and self._vision_ready():
                    await self._status(__event_emitter__, "Analyse des images de la page…")
                    prepared = []
                    for u in imgs[: self.valves.max_images * 3]:
                        if len(prepared) >= self.valves.max_images:
                            break
                        try:
                            fu, ic, ib, _ = await self._get(s, u, 10_000_000, IMAGE_ACCEPT)
                        except Exception:
                            continue
                        if not ic.startswith("image/") or "svg" in ic:
                            continue
                        data_url = self._prep_image(ib, ic)
                        if data_url:
                            prepared.append((fu, data_url))
                    if prepared:
                        image_section = await self._describe(
                            prepared, f"la page web « {title or final} »"
                        )

        except asyncio.TimeoutError:
            await self._status(__event_emitter__, "Timeout", True)
            return f"Timeout en lisant {url}"
        except (aiohttp.ClientError, ValueError) as e:
            await self._status(__event_emitter__, "Erreur", True)
            return f"Impossible de lire {url} : {e}"

        max_c = self.valves.max_chars
        out = f"URL : {final}\n"
        if title:
            out += f"Titre : {title}\n"
        if desc:
            out += f"Description : {desc}\n"
        out += f"\n{text[:max_c]}"
        if len(text) > max_c:
            out += f"\n\n[… texte tronqué à {max_c} caractères]"
        if not text and not desc and not image_section:
            out += "\n(Texte vide : page probablement générée en JavaScript.)"
        if image_section:
            out += f"\n\n--- Images de la page (analysées par vision) ---\n{image_section}"

        await self._status(__event_emitter__, "Page lue", True)
        return out

