# Spiral Warrior local backend distribution

This is a filtered, standalone copy of the current reconstructed Python backend. It is a local community compatibility server, not the original game service.

## Requirements and startup

- Windows 10/11 and Python 3.10 or newer.
- No third-party Python packages are required; the backend uses the standard library.

In PowerShell, from this folder:

```powershell
Copy-Item .\backend.env.example .\backend.env
# Optionally edit BACKEND_LAN_IP in backend.env to this PC's Wi-Fi IPv4 address.
.\start_backend.ps1
```

Or run the existing Python launcher directly:

```powershell
python .\run_local_server.py --lan
```

The launcher binds HTTP and WebSocket to `0.0.0.0`, detects the host PC's private IPv4 address, and advertises that address to the client. To set it explicitly:

```powershell
python .\run_local_server.py --lan --lan-ip <LAN_IP_OF_HOST_MACHINE>
```

`0.0.0.0` is only the server bind address; it is not a client destination. The phone and PC must be on the same LAN. Allow TCP ports **18765 (HTTP)** and **18766 (WebSocket)** only on the local subnet if Windows Firewall blocks them. The backend uses plaintext local testing endpoints and must not be exposed to the public internet.

The backend advertises the selected host address in its server/area responses. It cannot rewrite an address compiled into an Android APK. The client must already target the same host address (or use the appropriate emulator port forwarding). The APK is not included here, and its endpoint configuration is not verified by this package.

## Data and persistence

On first launch, the launcher creates fresh `data/local_accounts.sqlite3`, `data/accounts/`, and `logs/` runtime storage inside this folder. These files are not shipped. Do not copy an existing development database or account profiles into this package.

The `data` directory includes local catalogs required for the current recovered box, part-upgrade, level-progression, and Spiral Mart lottery behavior. They were derived from game-client resources and are included to make those local compatibility flows available. Their redistribution rights have not been reviewed; **do not upload this folder publicly until you confirm you may redistribute these derived catalogs**. This project has no assigned open-source license. The implementation also remains incomplete relative to the original backend.

Other catalogs absent from the current backend workspace (including a free-purchase catalog and some optional Adventure catalogs) remain unavailable or use the backend's existing fallback behavior. The package does not recreate them.

## Configuration

`backend.env.example` contains the optional client-facing LAN address placeholder consumed by `start_backend.ps1`. The bind address and ports are fixed by the copied launcher: bind `0.0.0.0`, HTTP `18765`, WebSocket `18766`. Leave the LAN address blank to auto-detect it, or enter the private IPv4 assigned to this host PC.

## Troubleshooting

- If startup says an address is invalid, confirm the entered IP is currently assigned to this PC.
- If a port is occupied, close the other local backend process or choose a different supported setup before launching. The Android client and advertised endpoint must use matching ports.
- The launcher checks HTTP `/healthz`; a successful health check does not prove the phone can connect.
- Keep the launcher console open while using the game. Press `Ctrl+C` to stop both services.
- For protocol or feature limitations, see the investigation reports in the development project; they are intentionally not copied into this runtime package.

## Included source

The Python service modules and seven existing backend regression-test modules were copied from `project/backend_local`. The packaged `run_local_server.py` differs only in setting its project root to this distribution folder, so runtime files are resolved here rather than from the original development tree.
