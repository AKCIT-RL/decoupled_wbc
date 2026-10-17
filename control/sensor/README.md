# Servidor de imagens (`composed_camera`)

Publica as câmeras do robô por ZMQ para que outras máquinas consumam: o vídeo
que vai para o headset de teleoperação e a observação que alimenta a política.

Roda **no robô**. Quem consome roda na workstation.

```
  [robô]  câmeras → composed_camera → ZMQ :5555 → [workstation] teleop / política
```

Este guia assume Linux e Python, e nada sobre o resto do repositório. Você não
precisa do projeto inteiro — só da pasta `control/sensor`.

---

## 1. O que você vai instalar

O repositório tem 366 MB, quase tudo irrelevante aqui (SDK da Manus, malhas de
URDF, simulador). O servidor de imagens usa 72 KB. Os passos abaixo baixam só
essa parte.

## 2. Pré-requisitos no robô

**Chave SSH com acesso ao repositório.** O repo é privado. Teste antes:

```bash
ssh -T git@github.com
```

Deve responder com o seu usuário. Se pedir senha ou negar, resolva a chave
primeiro — nada mais vai funcionar.

**Python 3.10+** e as bibliotecas:

```bash
pip install numpy opencv-python msgpack msgpack-numpy pyzmq gymnasium tyro
```

**`pyrealsense2`** — normalmente já vem instalado no robô. Confirme:

```bash
python -c "import pyrealsense2; print(pyrealsense2.__version__)"
```

Se faltar, `pip install pyrealsense2`.

## 3. Clonar

Clone parcial: baixa o histórico sem os arquivos grandes e materializa só as
pastas necessárias.

```bash
mkdir -p ~/camera && cd ~/camera

git clone --filter=blob:none --no-checkout git@github.com:AKCIT-RL/decoupled_wbc.git
cd decoupled_wbc
git sparse-checkout init --cone
git sparse-checkout set control/sensor control/base
git checkout integration/metaquest
```

**O branch tem que ser `integration/metaquest`.** O `main` não tem o driver da
RealSense.

Confira o que veio:

```bash
find . -name "*.py" | sort
```

Esperado:

```
./__init__.py
./control/__init__.py
./control/base/__init__.py
./control/base/env.py
./control/base/humanoid_env.py
./control/base/policy.py
./control/base/sensor.py
./control/sensor/__init__.py
./control/sensor/composed_camera.py
./control/sensor/oak.py
./control/sensor/realsense.py
./control/sensor/sensor_server.py
./version.py
```

Se faltar `realsense.py`, você está no branch errado.

Os arquivos de `control/base/` além de `sensor.py` vêm junto porque o
`sparse-checkout` traz diretórios inteiros. Nada os importa aqui; ficam parados
no disco.

## 4. Testar a câmera sozinha

Antes de subir o servidor, confirme que a RealSense responde. **A partir do
diretório pai** (`~/camera`, não de dentro de `decoupled_wbc`):

```bash
cd ~/camera
python -m decoupled_wbc.control.sensor.realsense --frames 60
```

Saída esperada, a cada 10 frames:

```
RealSense devices: ['335622072776']
Connected to RealSense 335622072776 as ego_view
ego_view: (480, 640, 3) uint8  latency 18.4 ms
```

O que olhar:

- **Shape `(480, 640, 3)` e dtype `uint8`.** Qualquer outra coisa é problema.
- **Latência estável.** Se oscilar muito ou passar de ~100 ms, a câmera está
  num link USB 2.0 saturado. Veja a seção 7.

Se aparecer `No RealSense devices found`, a câmera não está enumerada — veja a
seção 7.

## 5. Rodar o servidor

```bash
cd ~/camera
python -m decoupled_wbc.control.sensor.composed_camera \
    --ego-view-camera realsense \
    --port 5555 \
    --fps 30
```

Deixe rodando. Ele imprime estatísticas de envio a cada 100 mensagens.

**Sempre com `python -m`, sempre a partir do diretório pai.** Rodar
`python decoupled_wbc/control/sensor/composed_camera.py` falha: os imports são
absolutos e precisam do pai do pacote no `sys.path`.

### Mais de uma câmera

Cada posição de montagem vira uma chave na mensagem:

| flag | chave publicada |
|---|---|
| `--ego-view-camera` | `ego_view` |
| `--head-camera` | `head_left` e `head_right` (se binocular) |
| `--left-wrist-camera` | `left_wrist` |
| `--right-wrist-camera` | `right_wrist` |

Tipos aceitos: `realsense`, `oak`, `oak_mono`. (`zed` aparece no código mas o
driver não existe.)

Se houver mais de uma RealSense, desambigue pelo serial:

```bash
--ego-view-camera realsense --ego-view-device-id 335622072776
```

O serial aparece na linha `RealSense devices: [...]` do passo 4.

## 6. Verificar da workstation

Com o servidor rodando no robô, na workstation:

```bash
cd ~/camera    # mesma instalação, ou qualquer cópia do pacote
python -m decoupled_wbc.control.sensor.composed_camera \
    --no-run-as-server --port 5555
```

Imprime o FPS continuamente. Se não imprimir nada:

- o IP do robô está acessível? (`ping`)
- a porta 5555 está liberada? (firewall)
- o servidor está mesmo rodando no robô?

Esse cliente conecta em `localhost` por padrão. Para apontar para o robô, use o
consumidor real (o laço de teleoperação, com `--camera-host <ip-do-robô>`).

## 7. Quando der errado

| sintoma | causa provável |
|---|---|
| `ModuleNotFoundError: decoupled_wbc.control.sensor.realsense` | branch errado; use `integration/metaquest` |
| `ModuleNotFoundError: No module named 'decoupled_wbc'` | rodou de dentro da pasta; use `python -m` a partir do pai |
| `No RealSense devices found` | cabo, permissão de USB, ou a câmera está em uso por outro processo |
| `RealSense timeout` repetido | link USB saturado; veja abaixo |
| latência alta ou instável | o mesmo |
| cores invertidas no headset | convenção RGB/BGR no consumidor, não no servidor |

**Sobre o USB 2.0.** A RealSense deste robô costuma ficar num link USB 2.0, que
não tem banda para cor e profundidade ao mesmo tempo. Por isso a profundidade
vem **desligada por padrão** no `realsense.py`. Se você ligar e começar a ver
timeouts, é isso. Confirme o link com:

```bash
lsusb -t    # procure a RealSense e veja se diz 480M (USB 2.0) ou 5000M (USB 3.0)
```

**Câmera ocupada.** Só um processo por vez pode abrir a RealSense. Se o
teleimager ou outro servidor estiver rodando, mate antes.

## 8. Formato publicado, para referência

Quem consome recebe, por mensagem:

```python
{
  "timestamps": {"ego_view": 1700000000.123},   # segundos, relógio do host
  "images":     {"ego_view": ndarray(480,640,3) uint8 RGB},
}
```

Duas coisas que valem saber:

- **RGB, não BGR.** O driver converte antes de publicar, igual ao da OAK.
- **O timestamp é o instante de captura**, não o de recebimento. O driver liga
  `global_time_enabled` na câmera para alinhar com o relógio do host. Isso é o
  que permite casar imagem com ação ao gravar dados de treino.
