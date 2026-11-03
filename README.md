# Mikoshi

App de escritorio para catalogar películas, música, fotos, libros y juegos que
están en una carpeta local o de red.

## Puesta en marcha

Requiere Python 3.12 o superior. `ffmpeg` es necesario para los vídeos.

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m mikoshi
```

En Windows también se puede abrir con `Abrir Mikoshi.bat`. Pruebas:
`.venv\Scripts\python -m pytest tests`.

## Crear el programa

`powershell -ExecutionPolicy Bypass -File packaging\build.ps1` genera
`dist\Mikoshi\Mikoshi.exe`. Si Inno Setup 6 está instalado, también crea el
instalador en `dist`. El reproductor va incluido, pero leer datos de vídeo, crear
sus miniaturas y escribir en ellos sigue necesitando `ffmpeg` en el PATH.

## Secciones

Mikoshi se organiza en pestañas, cada una con su propio diseño:

- **Películas y series** (al estilo de Jellyfin): inicio con «Continuar viendo»,
  pósteres de películas, series agrupadas por temporada y episodio, marca de
  visto y reanudación donde lo dejaste. Un vídeo con `S01E02` en el nombre o con
  serie cuenta como episodio; se puede corregir en *Clasificación*.
- **Música** (al estilo de MusicBee): navegación por artista, álbum, género o
  año, orden por álbum y pista, y una barra de reproducción fija con cola,
  aleatorio y repetición que sigue sonando mientras navegas.
- **Videos favoritos** (al estilo de YouTube): cuadrícula 16:9 con duración,
  página de reproducción y «A continuación». Usa la carpeta marcada como de
  favoritos; si una sola carpeta se llama así, se asigna sola.
- **Libros** (al estilo de Calibre): explorador por autor, serie, género,
  editorial y etiqueta, tabla y ficha con portada y descripción.
- **Juegos** (al estilo de Steam): biblioteca de portadas a partir de los
  accesos directos (`.lnk`, `.url`), botón *Jugar*, última partida y portadas
  de Steam sin clave para los accesos de Steam.
- **Todo**: la vista de gestión de siempre, con barra lateral y edición.

## Funciones

- **Datos de internet:** TMDB (películas y series), MusicBrainz (música), Open
  Library (libros) y RAWG (juegos). TMDB y RAWG piden una clave gratuita en
  *Archivo > Ajustes*. Solo se conecta cuando pulsas *Buscar en internet*.
- **Comprobación de metadatos:** después de *Escribir en archivo*, Mikoshi
  vuelve a leer cada archivo y muestra los valores que contiene de verdad. El
  botón *Comprobar qué hay en el archivo* hace lo mismo en cualquier momento.
- **Edición masiva:** selecciona varios elementos y pulsa *Editar varios*. Solo
  cambia los campos marcados y solo en el catálogo.
- **Organización:** playlists con orden propio, colecciones, series, álbumes y
  filtros por valoración, género y año. Las carpetas de *Mis listas* solo agrupan
  listas dentro de Mikoshi: no crean ni mueven nada en el disco.
- **Barra lateral:** el clic derecho permite crear, renombrar, mover y borrar
  listas, carpetas y etiquetas, y escanear o gestionar las carpetas en disco.
- **Mantenimiento:** *Comprobar integridad* detecta archivos dañados comparando
  huellas SHA-256; *Buscar duplicados* encuentra contenido idéntico. Ninguno
  modifica ni borra archivos.
- **Copia automática:** al cerrar, como máximo una vez al día, en la carpeta
  elegida en *Ajustes*. Las copias incluyen las claves de los servicios.
- **Reproductor:** vídeo, música y fotos dentro de la app, con *Abrir fuera* para
  lo que no pueda reproducir.

## Cómo guarda los datos

- El catálogo, las miniaturas y las portadas están en
  `%LOCALAPPDATA%\Mikoshi`, no en el servidor, porque SQLite no es
  fiable en carpetas de red. *Archivo > Copia del catálogo* lo copia al servidor.
- Editar solo cambia el catálogo. Los archivos cambian únicamente con
  *Escribir en archivo*, y nunca se borran.
- Un servidor apagado no hace que nada se marque como faltante.
- Si cambia la ruta del servidor, usa *Archivo > Gestionar carpetas > Cambiar
  ruta*. Conviene usar rutas UNC (`\\servidor\media`) en vez de letras de unidad.

## Escritura en archivos

| Tipo | Formatos | Campos |
|---|---|---|
| Música | MP3, FLAC, OGG, Opus, M4A | título, artista, álbum, año, pista, género |
| Vídeo | MKV | título, año, género, descripción, director, serie, temporada, episodio |
| Vídeo | MP4, M4V, MOV | los mismos, sin director |
| Fotos | JPEG, sin recodificar los píxeles | título, autor, descripción |

Lo demás, incluidos libros y juegos, se guarda solo en el catálogo. Los campos
que se pueden escribir llevan `*` en el formulario.

## Juegos

Usa *Añadir carpeta de juegos*: cada subcarpeta cuenta como un juego, y también
cada archivo suelto de juego (ROM, ISO, ZIP, EXE...). En una carpeta normal solo
se reconocen las ROM por su extensión.
