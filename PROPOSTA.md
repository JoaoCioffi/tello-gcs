# Tello Ground Station: proposta de projeto

> Mac (M4, 24 GB) como ground station com todo o processamento. O Tello só voa, transmite vídeo e telemetria e recebe comandos.
> Objetivo: ter um sistema completo, de ponta a ponta, em **3 fases curtas + 1 bônus**: teleoperação → percepção 3D → voo autônomo → anomalias e agente.

---

## 1. O que dá e o que não dá para fazer com o Tello (expectativas)

| Item | Realidade |
|---|---|
| Sensores úteis | Câmera frontal monocular (720p a 30 fps, H.264 via UDP 11111, FOV de cerca de 82°, rolling shutter). Estado a cerca de 10 Hz em UDP 8890: pitch, roll e yaw, velocidades `vgx/vgy/vgz` (do VPS/fluxo óptico), altura pelo ToF, barômetro, acelerações, bateria e temperatura |
| "LiDAR de câmera" | **Sim, mas é uma pseudo-LiDAR**: profundidade monocular métrica (Depth Anything V2 Metric Indoor) convertida em nuvem de pontos. O erro esperado fica entre 10% e 20%. Serve para mapear e desviar de obstáculos na escala de um cômodo, não serve para levantamento de precisão |
| SLAM | ORB-SLAM3 monocular é o caminho com mais casos comprovados no Tello (tau-adl, ResearchGate). Monocular não tem escala: **a escala vem do ToF e das velocidades do VPS** |
| VIO "de verdade" | Não é viável. A IMU não chega em alta taxa nem com timestamp sincronizado. A fusão será fraca (EKF com pose visual + odometria do Tello) |
| Stereo DSO / DSO / SVO | Bons para estudo. Stereo DSO exige estéreo, e DSO direto sofre com rolling shutter e autoexposição. Ficam fora do caminho crítico |
| RuView | Usa CSI de Wi-Fi com ESP32. **Não se aplica ao Tello**, porque o chip dele não expõe CSI. Pode virar um projeto paralelo (presença na casa), fora deste escopo |
| Latência | Vídeo com 150 a 300 ms de atraso. Por isso o controle autônomo tem que ser lento e conservador |
| Autonomia | Cerca de 8 a 10 min reais por bateria. Daí a importância de **gravar e reproduzir** voos (seção 3) |
| Ambiente | O VPS precisa de luz e de piso com textura. Em piso liso e claro o drone deriva, então vale usar um tapete. Use protetor de hélice |

**Rede:** com o Mac conectado ao Wi-Fi do Tello, o Mac fica sem internet. Para baixar pesos ou chamar uma LLM na nuvem durante o voo, dá para usar tethering USB do iPhone ou um adaptador Ethernet. Os modelos Tello EDU/TT têm modo station e entram no seu roteador; o Tello comum não tem.

---

## 2. Arquitetura

```
 Tello ──Wi-Fi UDP──►  MacBook (Ground Station)
  vídeo  :11111   ─┐   ┌──────────────────────────────────────────────────────────┐
  estado :8890    ─┼──►│ link/        djitellopy + PyAV, watchdog, rc a 30 Hz      │
  cmd    :8889  ◄──┘   │ control/     PS5 / teclado → ÁRBITRO (manual > autônomo)  │
                       │              failsafes: bateria, perda de link, teto      │
                       │ perception/  YOLO11 (MPS) + Depth Anything V2 métrico     │
                       │ slam/        pose L0: odometria Tello │ L1: ORB-SLAM3     │
                       │ mapping/     pseudo-LiDAR → voxel 3D + objetos ancorados  │
                       │ planning/    A* 3D, seguidor de waypoints, freio reativo  │
                       │ health/      LSTM autoencoder (anomalia em telemetria)    │
                       │ agent/       LLM com tools sobre o world model            │
                       │ ui/          HUD pygame (vídeo + overlay) + Rerun (3D)    │
                       │ recorder/    grava tudo, permite replay offline           │
                       └──────────────────────────────────────────────────────────┘
```

Princípios:
1. **Barramento de mensagens interno** (pub/sub com mensagens tipadas e timestamp). No começo roda com threads e filas de "último frame". Se o GIL apertar, migra para ZeroMQ entre processos sem reescrever os módulos.
2. **Árbitro de controle**: qualquer toque no stick do PS5 tira o controle da autonomia na hora. O botão de emergência sempre funciona.
3. **Autonomia e LLM ficam fora do loop rápido.** O loop de 30 Hz é determinístico. A LLM só propõe missões, que passam pelo planner e pelo filtro de segurança.
4. **Gravar e reproduzir desde o dia 1**: todo voo gera um dataset (frames, estado, comandos, timestamps). O pipeline inteiro roda sobre gravações, então dá para desenvolver mapeamento, planner e LSTM sem gastar bateria.

### Stack proposta (nada instalado ainda)
- **Python 3.12** num venv novo, com `uv`. O venv atual é 3.9, que já está em EOL e trava libs novas.
- Já existentes: `djitellopy`, `av`, `opencv-python`, `pygame` (DualSense funciona via SDL2), `ultralytics`, `torch` (MPS)
- Novas: `rerun-sdk` (viewer 3D, séries temporais e replay), `transformers` (Depth Anything V2), `open3d` (voxel/TSDF, opcional), `scipy`
- SLAM: **pyslam** (tem suporte a macOS e encapsula a lógica do ORB-SLAM) num processo separado. A alternativa é compilar o ORB-SLAM3. Esta é a dependência mais arriscada, por isso existe o L0 (abaixo).

---

## 3. Fases

### Fase 1: Voar e enxergar
- Link robusto: conexão, keepalive, stream de vídeo com decode PyAV em thread própria, estado a 10 Hz, `rc` a 30 Hz
- Controle por **PS5 e teclado** com deadzone, curva expo e modo lento/rápido. Botões para decolar, pousar, emergência e gravar
- Failsafes: pouso automático com bateria abaixo de 15%, pouso se o link cair por mais de 1 s, teto de altura e bloqueio de decolagem com bateria abaixo de 25%
- HUD: vídeo com caixas do YOLO11, bateria, altura (ToF), atitude, velocidades, latência e FPS
- **Recorder**: cada voo vira uma pasta reproduzível
- Calibração intrínseca da câmera com tabuleiro de xadrez (necessária para a Fase 2)

✅ **Pronto quando:** você voa 5 min com o PS5 e vê as detecções a pelo menos 15 fps, e o voo fica gravado e reproduzível sem o drone.

### Fase 2: Mapear (pseudo-LiDAR + pose + objetos 3D)
- Profundidade métrica por frame-chave (cerca de 5 a 10 Hz, no MPS), convertida em nuvem de pontos com os intrínsecos calibrados
- Pose em duas camadas:
  - **L0, dia 1:** integração de `vgx/vgy` + yaw + altura do ToF. Funciona logo, com deriva de cerca de 10% a 20%
  - **L1:** ORB-SLAM3 monocular (pyslam) com escala ancorada no ToF e no VPS, mais loop closure. EKF simples funde L0 e L1
- Mapa: grade de voxels 3D (5 cm) com ocupado, livre e desconhecido, alimentada por ray-casting
- Objetos: YOLO + tracking (ByteTrack) + profundidade no centro da caixa, o que dá um **landmark semântico** (por exemplo, "cadeira #2 em x, y, z") com deduplicação
- Visualização no Rerun: trajetória, nuvem, voxels e objetos em 3D, ao vivo ou em replay

✅ **Pronto quando:** depois de uma volta pela sala, existe um mapa 3D reconhecível e uma lista de objetos com posição (erro em torno de 30 cm ou menos).

### Fase 3: Voar sozinho
- Planner: A* sobre a grade de voxels inflada (margem de segurança), mais suavização de caminho
- Seguidor de waypoints: PID de posição e yaw convertido em `rc`, com velocidade máxima baixa (0,3 m/s)
- Freio reativo: se a profundidade no cone frontal ficar abaixo de 0,6 m, o drone para e replaneja
- Missões pela UI: clicar no mapa (go-to), **"vá até o objeto X"** (navegação semântica), exploração por fronteiras e **return-to-home**
- Árbitro: qualquer intervenção no PS5 pausa a missão

✅ **Pronto quando:** o drone faz a missão A → B → home desviando de um obstáculo, sem toque manual.

### Fase 4 (bônus): Saúde do voo e voo agêntico
- **LSTM autoencoder** sobre janelas de telemetria (atitude, velocidades, comandos, ToF, barômetro, bateria e temperatura), treinado com os voos "normais" gravados nas fases anteriores. O erro de reconstrução vira um score de anomalia no HUD, que aciona RTH ou pouso acima de um limiar. Retoma a linha do seu TCC (detecção de mau funcionamento a partir da dinâmica temporal) com dado real
  - Anomalias para testar: hélice danificada ou desbalanceada, rajada de ventilador, toque ou colisão, deriva do VPS em piso liso
- **Agente**: LLM com tools tipadas (`list_objects`, `get_map_summary`, `goto(obj|xyz)`, `look_around`, `rth`, `land`) que transforma "procure minha mochila na sala e volte" num plano. Toda chamada passa pelo planner e pelos failsafes. Opções: Claude API (precisa de internet, ver seção 1) ou LLM local via MLX/Ollama no M4

✅ **Pronto quando:** o HUD sinaliza uma anomalia induzida e o drone executa uma missão em linguagem natural de ponta a ponta.

---

## 4. Estrutura do repositório

```
tello_gcs/
  bus.py            mensagens tipadas + pub/sub
  link/             drone.py, video.py, state.py, sim.py (drone fake p/ testes)
  control/          gamepad.py, keyboard.py, arbiter.py, failsafe.py
  perception/       detector.py, depth.py, calib.py
  slam/             odom_l0.py, orbslam_l1.py, fusion.py
  mapping/          pointcloud.py, voxel_map.py, objects.py
  planning/         astar.py, follower.py, reactive.py, missions.py
  health/           lstm_ae.py, train.py
  agent/            tools.py, agent.py
  ui/               hud.py, rerun_view.py
  recorder/         record.py, replay.py
app.py              ponto de entrada (--live | --replay <pasta>)
```

## 5. Riscos e mitigação
| Risco | Mitigação |
|---|---|
| Build do ORB-SLAM3/pyslam no macOS ARM | L0 garante pose desde o dia 1, e o L1 é um upgrade |
| Deriva em piso liso ou pouca luz | Tapete com textura, luz boa, voos curtos |
| Latência e perda de frames | Filas de "último frame", processar só frames-chave, velocidade autônoma baixa |
| Superaquecimento do Tello parado no chão com stream ligado | Ligar o stream só perto da decolagem |
| Bateria curta | Gravar e reproduzir, e desenvolver offline |
