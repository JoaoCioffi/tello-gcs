# Tello Ground Station

Ground station no Mac para o DJI Tello. O Mac faz todo o processamento; o drone só voa,
transmite vídeo e telemetria e recebe comandos. Plano completo em [PROPOSTA.md](PROPOSTA.md).

**Fase 1 (atual):** teleoperação PS5/teclado, HUD com telemetria e latência, YOLO no GPU
(MPS), failsafes, gravação de todo voo, replay, pré-voo rápido, medição de latência e
calibração de câmera. Tudo testável **sem ligar o drone** (emulador UDP incluído).

## Setup

```bash
uv sync                      # Python 3.12 + dependências (.venv)
uv run pytest                # 25 testes (unidade + integração com o emulador)
```

Os pesos `models/yolo26n.pt` já estão baixados (o Mac fica sem internet no Wi-Fi do Tello).

## Ensaio sem drone (recomendado antes do 1º voo)

```bash
uv run python app.py --sim
```

Abre o HUD com um Tello simulado numa sala 3D. Use para treinar os comandos e **conferir o
mapeamento do PS5**. Parear o DualSense: segure **PS + Create** até a luz piscar, depois
Ajustes do Sistema → Bluetooth (ou use cabo USB-C).

Opções úteis: `--sim-video-delay-ms 150` (simula atraso de vídeo), `--sim-sdk3` (simula EDU/TT).

## Roteiro do 1º voo (cabe numa bateria)

O Tello parado e ligado esquenta, então a ideia é: deixar tudo pronto, ligar e voar.

1. **Antes de ligar:** controle pareado, área livre de ~2×2 m, piso com textura (tapete),
   boa luz, protetor de hélice. Opcional: abra `calib/checkerboard.png` em tela cheia
   (para calibrar a câmera durante o voo).
2. **Ligue o drone** e conecte o Mac no Wi-Fi `TELLO-XXXXXX`.
   Na 1ª vez o macOS pode perguntar se o Python pode **aceitar conexões de entrada**:
   clique em **Permitir** (sem isso não chegam vídeo nem telemetria).
3. **Pré-voo (~5 s):**
   ```bash
   uv run python preflight.py
   ```
   Saída esperada: `GO`. O stream é desligado no fim.
4. **Voo:**
   ```bash
   uv run python app.py
   ```
   - Checklist do painel mostrando **PRONTO P/ VOO: GO**
   - △ (ou `T`) decola, hover de ~10 s observando o HUD
   - Movimentos suaves no modo lento (30%). D-pad ↑/↓ troca a velocidade
   - (Opcional) ~15 s apontando para o tabuleiro de ângulos e distâncias variados
   - ✕ (ou `L`) pousa. `Esc` sai (pousa antes se estiver voando)
5. **Depois do voo** (drone já desligado):
   ```bash
   uv run python app.py --replay recordings/<sessão>
   ```
   ```bash
   uv run python tools/calibrate.py --recording recordings/<sessão> --square-mm <lado medido>
   ```

Latência real de ponta a ponta (opcional, +8 s): `uv run python preflight.py --latency` com a
câmera apontada para a janela que pisca.

## Controles

| Ação | PS5 | Teclado |
|---|---|---|
| Decolar | △ Triângulo | `T` |
| Pousar | ✕ Xis | `L` |
| **EMERGÊNCIA** (corta motores, o drone cai) | **Create** | **Backspace** |
| Frente / trás (pitch) | stick esquerdo ↑ / ↓ | `W` / `S` |
| Direita / esquerda (roll) | stick esquerdo → / ← | `D` / `A` |
| Sobe / desce | R2 / L2 (proporcional ao gatilho) | `↑` / `↓` |
| Yaw direita / esquerda | R1 / L1 (80% da velocidade atual) | `→` / `←` |
| Velocidade (30/60/100%) | D-pad ↑ / ↓ | `1` `2` `3` |
| Foto | □ Quadrado | `P` |
| Gravar on/off | ○ Círculo | `R` |
| Overlay de detecção | Options | `O` |
| Sair | | `Esc` |

O painel mostra a **bateria do controle** (em CONTROLE), atualizada a cada 5 s, com alerta
no HUD quando cair a 20% ou menos. O pré-voo também mostra esse valor.

O stick direito está livre. O mapeamento fica em `tello_gcs/control/mapping.py`, e as
sensibilidades (zona morta, expo, yaw) em `tello_gcs/config.py`.
O controle funciona mesmo com a janela fora de foco.

## Segurança (automática)

- **Entrada travada** (UI sem atualizar por 250 ms): hover
- **Link perdido** (sem telemetria por 1 s): hover. O próprio Tello pousa após 15 s sem comandos
- **Bateria ≤ 12%**: pouso automático. Decolagem bloqueada abaixo de 20%
- **Teto de 200 cm** (`--max-height`): bloqueia subida e desce se passar de 230 cm
- **Temperatura**: alerta a partir de 75 °C, crítico a partir de 85 °C
- **Fechar a janela voando**: pousa antes de sair
- Manual sempre tem prioridade sobre a autonomia (o árbitro já está pronto para a Fase 3)

## Latência: o que já está otimizado

- Driver UDP próprio. `rc` é enviado a 30 Hz numa thread dedicada, sem esperar resposta
- Vídeo: o fim do frame é detectado pelo tamanho do datagrama (não espera o próximo start
  code, economiza ~33 ms), decoder com LOW_DELAY e threading por slice
- Frames com semântica "só o último" (`Latest`): ninguém processa frame velho
- O HUD mostra o atraso do lado do Mac (frame→tela) e o pré-voo mede a latência ponta a ponta

## Estrutura

```
app.py                  ground station (HUD + controle + YOLO + gravação)
preflight.py            pré-voo rápido / medição de latência
tools/                  calibrate.py, make_checkerboard.py
tello_gcs/
  link/                 driver UDP, protocolo, recepção/decodificação de vídeo
  control/              entradas PS5/teclado, árbitro, failsafe, loop de voo
  perception/           detector YOLO, latência, calibração
  recorder/             gravação, leitura offline, replay
  sim/                  emulador do Tello + cena 3D
  ui/                   HUD (pygame)
recordings/<sessão>/    video.bin, state.jsonl, cmds.jsonl, events.jsonl, meta.json
```

## Observações

- Bateria do DualSense: o macOS não informa a bateria dele. A app pede ao driver do SDL
  o "modo estendido" do controle (`SDL_JOYSTICK_HIDAPI_PS5_RUMBLE=1`) e lê o byte de
  bateria via `hidapi` em modo **não exclusivo**, sem tirar o controle do pygame. A precisão
  é de 10%, a mesma do próprio controle. O controle fica nesse modo até ser reconectado
- OpenCV está fixado em `<4.13` com a variante headless. A partir da 4.13 ele embute
  SDL2/libavdevice, que colidem com os do pygame/PyAV no macOS
- Unidades de `vgx/vgy/vgz` (dm/s) e o frame de referência delas serão confirmados com dados
  reais na Fase 2

## Status

- **Fase 1:** completa e validada no simulador (incluindo o mapeamento do PS5).
  **Ainda não voou com o drone real.** O modelo do Tello (comum ou EDU/TT) também não foi
  identificado; o `preflight.py` descobre isso via `sdk?`
- **Próximo passo:** primeiro voo real seguindo o roteiro acima. Depois, analisar a gravação
  (latência, telemetria, calibração) e começar a Fase 2 (ver [PROPOSTA.md](PROPOSTA.md))

## Rede: internet e Tello ao mesmo tempo

A placa Wi-Fi conecta numa rede só. Ao entrar no `TELLO-XXXXXX`, a máquina perde a internet.
O app voa 100% offline (os pesos YOLO ficam em `models/`), mas para usar o Claude ou baixar
coisas durante o voo, a internet precisa chegar **por outro caminho**:

- **Hotspot do iPhone pelo cabo USB**, não pelo Wi-Fi, que fica ocupado com o Tello. O
  hotspot usa dados móveis. No Mac, coloque o "iPhone USB" acima do Wi-Fi em Ajustes →
  Rede → "…" → Ordem dos Serviços. No Windows, precisa do driver da Apple (veja abaixo)
- **Adaptador USB-C → Ethernet** ligado ao roteador
- **Segundo adaptador Wi-Fi USB:** uma placa no Wi-Fi de casa, outra no Tello. Funciona bem
  no Windows (no macOS, não), não gasta dados móveis e o adaptador pode ficar num extensor
  perto da área de voo
- **Modo station** (o drone entra no seu roteador): só existe no Tello EDU/TT

Em máquinas corporativas com agentes de segurança (ex.: Netskope), o Claude pode ser
bloqueado em redes não confiáveis, como o hotspot. Isso é política da empresa; não contorne.

## Migração para Windows (pendências)

O projeto vai continuar num desktop **Windows com GPU AMD Radeon RX 7800 XT (16 GB)** e
Wi-Fi. O código foi escrito e testado só no macOS. Antes do primeiro voo no Windows, é
preciso resolver:

**Código**
- [ ] **UDP `ConnectionResetError`:** no Windows, um pacote UDP recusado (ICMP port
  unreachable) faz a próxima `recvfrom` lançar `ConnectionResetError` (WSAECONNRESET). As
  threads em `link/driver.py`, `link/video.py`, `sim/emulator.py` e `recorder/replay.py`
  tratam `OSError` com `break` e morreriam. Correção: desligar com
  `sock.ioctl(socket.SIO_UDP_CONNRESET, False)` ao criar o socket, e/ou tratar
  `ConnectionResetError` com `continue`
- [ ] **Relógio:** no Python 3.12/Windows, `time.monotonic()` tem resolução de ~15,6 ms
  (GetTickCount64), o que estraga as medições de latência e RTT. Trocar todos os timestamps
  por `time.perf_counter()` ou subir para Python 3.13, onde o `monotonic` usa QPC
- [ ] **GPU AMD:** `Detector` hoje escolhe `mps` ou `cpu`. Opções, em ordem de preferência:
  1. PyTorch com ROCm nativo no Windows (a AMD tem liberado para a série RX 7000; conferir se
     a 7800 XT, gfx1101, está na lista atual). Com ROCm, o PyTorch expõe a GPU como `cuda`
  2. Exportar o YOLO para ONNX (`yolo export format=onnx`) e rodar com
     `onnxruntime-directml` (qualquer GPU DX12)
  3. CPU (YOLO26n roda a ~20–40 ms num processador desktop moderno; serve para a Fase 1)

  O `torch` do PyPI no Windows é só CPU; a variante ROCm vem do índice da AMD/PyTorch
  (configurar em `[tool.uv.sources]`). Adicionar a flag `--device`
- [ ] **Bateria do DualSense:** em `control/pad_battery.py`, o truque via ctypes
  (`hid_darwin_set_open_exclusive`) é específico do macOS e pode quebrar o carregamento no
  Windows (os símbolos C não são exportados do `.pyd`). No Windows, o HID já abre em modo
  compartilhado: pular o ctypes quando `sys.platform != "darwin"`. O fallback (nível
  aproximado do SDL) já existe
- [ ] **Fonte do HUD:** `SysFont("menlo")` só existe no Mac. Usar uma lista:
  `"menlo,consolas,dejavusansmono"`
- [ ] **DPI:** em telas com escala, a janela do pygame fica borrada. Chamar
  `ctypes.windll.user32.SetProcessDPIAware()` no início
- [ ] **Encoder do emulador:** usa `libx264` via PyAV. Se o wheel do Windows não tiver,
  cair para `h264_mf` (Media Foundation) ou `libopenh264`
- [ ] **Cores ANSI no `preflight.py`:** no console antigo do Windows, habilitar com
  `os.system("")`; no Windows Terminal já funciona

**Ambiente**
- [ ] `uv sync`. Os pesos `models/*.pt` não estão no git; baixar de novo (o ultralytics
  baixa sozinho se faltar, com internet)
- [ ] **Firewall:** o Windows classifica a rede `TELLO-XXXX` como **Pública** e bloqueia o
  UDP de entrada (vídeo em 11111 e telemetria em 8890). Marcar a rede como **Privada** ou
  liberar o Python no firewall. No primeiro `app.py --sim` também aparece o aviso do firewall
- [ ] **Hotspot USB do iPhone:** instalar **Apple Devices** (Microsoft Store) ou iTunes, que
  traz o driver "Apple Mobile Device Ethernet". Se a internet cair ao entrar no Wi-Fi do
  Tello, ajustar a métrica das interfaces
- [ ] **PS5:** parear por Bluetooth nas configurações do Windows. O SDL suporta bem o
  DualSense no Windows, inclusive bateria
- [ ] Rodar `uv run pytest` e `uv run python app.py --sim` antes de ligar o drone

**Fase 2 no Windows**
- ORB-SLAM3 / pyslam são feitos para Linux. O caminho provável é rodar o SLAM no **WSL2** e
  o app (link, HUD, controle) nativo no Windows, ligados por ZeroMQ (o barramento interno já
  foi pensado para isso). O WSL2 em modo NAT não recebe bem o UDP do Tello, por isso o link
  fica no Windows
