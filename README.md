# Catálogo Multimedia

App de escritorio para catalogar películas, música, fotos, libros y juegos que
están en una carpeta local o de red.

## Puesta en marcha

Requiere Python 3.12 o superior. `ffmpeg` es necesario para los vídeos.

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m catalogo
```

En Windows también se puede abrir con `Abrir catalogo.bat`. Pruebas:
`.venv\Scripts\python -m pytest tests`.

## Cómo guarda los datos

- El catálogo, las miniaturas y las portadas están en
  `%LOCALAPPDATA%\CatalogoMultimedia`, no en el servidor, porque SQLite no es
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
