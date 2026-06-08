# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for ScanToBIM standalone distribution (no license system).
# Run:  .venv\Scripts\python.exe -m PyInstaller ScanToBIM.spec --noconfirm

from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

# ── Collect packages that have DLLs / data files ──────────────────────────────
open3d_d, open3d_b, open3d_h  = collect_all("open3d")
ifc_d,    ifc_b,    ifc_h     = collect_all("ifcopenshell")
pye57_d,  pye57_b,  pye57_h  = collect_all("pye57")
rdflib_d, rdflib_b, rdflib_h = collect_all("rdflib")

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ["webapp/_launcher.py"],
    pathex=["webapp"],          # makes `import backend.*` resolve
    binaries=open3d_b + ifc_b + pye57_b + rdflib_b,
    datas=[
        # Bundle the HTML/CSS/JS frontend alongside the exe
        ("webapp/frontend", "frontend"),
    ] + open3d_d + ifc_d + pye57_d + rdflib_d,
    hiddenimports=[
        # ── uvicorn internals (loaded via importlib strings) ──
        "uvicorn.logging",
        "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
        "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan", "uvicorn.lifespan.off", "uvicorn.lifespan.on",
        # ── FastAPI / middleware ──
        "fastapi", "fastapi.staticfiles", "fastapi.responses",
        "fastapi.middleware.cors",
        # ── async file IO ──
        "aiofiles", "aiofiles.os", "aiofiles.threadpool",
        # ── backend package & all services ──
        "backend", "backend.main", "backend.state",
        "backend.services",
        "backend.services.construction_kg",
        "backend.services.deviation_service",
        "backend.services.e57_loader",
        "backend.services.export_service",
        "backend.services.ifc_service",
        "backend.services.model_kg",
        "backend.services.registration_service",
        "backend.services.segmentation_service",
        "backend.services.mqa_assessment",
    ] + open3d_h + ifc_h + pye57_h + rdflib_h,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib.tests", "numpy.tests", "scipy.tests"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScanToBIM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # Shows a console window — useful to see errors
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ScanToBIM",
)
