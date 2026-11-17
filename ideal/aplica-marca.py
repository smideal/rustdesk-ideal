#!/usr/bin/env python3
"""Aplica a marca IdealAcesso num checkout do rustdesk/rustdesk (tag 1.5.3).

Uso:
    RS_PUB_KEY=<chave> python3 aplica-marca.py <caminho-do-checkout-rustdesk>

Variáveis de ambiente (lidas na hora de aplicar e gravadas no código-fonte, porque
o RustDesk 1.5.3 NÃO lê option_env!("RENDEZVOUS_SERVER"/"RS_PUB_KEY"/"API_SERVER")
em tempo de compilação; ver o README):
    RS_PUB_KEY         obrigatória: conteúdo do id_ed25519.pub do hbbs (base64, 32 bytes).
    RENDEZVOUS_SERVER  opcional: servidor de ID (padrão homologa.supermercadoideal.com.br).
    API_SERVER         opcional: API do painel (padrão
                       https://acesso-remoto.187.53.110.196.sslip.io).

Só biblioteca padrão. Idempotente: rodar de novo não muda nada. Se um trecho
esperado não for achado (o upstream mudou), para com erro e não grava nada.
"""

import argparse
import base64
import os
import re
import shutil
import sys
from pathlib import Path

NOME_APP = "IdealAcesso"
SERVIDOR_PADRAO = "homologa.supermercadoideal.com.br"
API_PADRAO = "https://acesso-remoto.187.53.110.196.sslip.io"
EMPRESA = "Supermercado Ideal"
DESCRICAO = "IdealAcesso - acesso remoto do Supermercado Ideal (baseado no RustDesk)"


class TrechoNaoAchado(Exception):
    pass


def falha(msg):
    print(f"ERRO: {msg}", file=sys.stderr)
    sys.exit(1)


def le_parametros():
    chave = os.environ.get("RS_PUB_KEY", "").strip()
    servidor = os.environ.get("RENDEZVOUS_SERVER", "").strip() or SERVIDOR_PADRAO
    api = os.environ.get("API_SERVER", "").strip() or API_PADRAO

    if not chave:
        falha("defina RS_PUB_KEY com o conteúdo do id_ed25519.pub do hbbs.")
    try:
        if len(base64.b64decode(chave, validate=True)) != 32:
            raise ValueError
    except ValueError:
        falha("RS_PUB_KEY não é uma chave ed25519 em base64 (32 bytes); "
              "copie o conteúdo do id_ed25519.pub do hbbs, sem quebra de linha.")
    if not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d{1,5})?", servidor):
        falha(f"RENDEZVOUS_SERVER inválido: {servidor!r} (esperado host ou host:porta).")
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?", api):
        falha(f"API_SERVER inválido: {api!r} (esperado http(s)://host[:porta][/caminho]).")
    api = api.rstrip("/")
    return chave, servidor, api


def troca(texto, padrao, novo, descricao, flags=0):
    """Substitui exatamente uma ocorrência de `padrao` (regex com grupos 1 e 2 em
    volta do valor) por grupo1 + novo + grupo2. Falha se achar 0 ou mais de 1."""
    achados = list(re.finditer(padrao, texto, flags))
    if len(achados) != 1:
        raise TrechoNaoAchado(
            f"{descricao}: esperado 1 trecho, achados {len(achados)} "
            f"(a versão do upstream mudou? padrão: {padrao!r})")
    m = achados[0]
    return texto[:m.start()] + m.group(1) + novo + m.group(2) + texto[m.end():]


def bloco_da_funcao(texto, assinatura):
    """Devolve (inicio, fim) do corpo de uma função Rust de nível de módulo
    (da assinatura até a primeira linha '}' na coluna 0)."""
    ini = texto.find(assinatura)
    if ini < 0 or texto.find(assinatura, ini + 1) >= 0:
        raise TrechoNaoAchado(f"função {assinatura!r} não achada (ou achada mais de uma vez)")
    fim = texto.find("\n}\n", ini)
    if fim < 0:
        raise TrechoNaoAchado(f"fim da função {assinatura!r} não achado")
    return ini, fim + 3


def edicoes(chave, servidor, api):
    """Lista de (arquivo relativo, função que recebe o texto e devolve o novo)."""
    def config_rs(t):
        t = troca(t, r'(pub static ref APP_NAME: RwLock<String> = RwLock::new\(")[^"]*("\.to_owned\(\)\);)',
                  NOME_APP, "APP_NAME (hbb_common/config.rs)")
        t = troca(t, r'(pub const RENDEZVOUS_SERVERS: &\[&str\] = &\[)[^\]]*(\];)',
                  f'"{servidor}"', "RENDEZVOUS_SERVERS (hbb_common/config.rs)")
        t = troca(t, r'(pub const RS_PUB_KEY: &str = ")[^"]*(";)',
                  chave, "RS_PUB_KEY (hbb_common/config.rs)")
        return t

    def common_rs(t):
        # O último recurso de get_api_server_() é "https://admin.rustdesk.com":
        # sem a opção api-server e sem custom-rendezvous-server, o cliente cairia nele.
        ini, fim = bloco_da_funcao(t, "fn get_api_server_(api: String, custom: String) -> String {")
        corpo = troca(t[ini:fim], r'(\n    ")[^"\n]*("\.to_owned\(\)\n\}\n)$',
                      api, "padrão do API server em get_api_server_ (src/common.rs)")
        return t[:ini] + corpo + t[fim:]

    def runner_rc(t):
        for campo, valor in (("CompanyName", EMPRESA),
                             ("FileDescription", DESCRICAO),
                             ("InternalName", NOME_APP),
                             ("OriginalFilename", f"{NOME_APP}.exe"),
                             ("ProductName", NOME_APP)):
            t = troca(t, rf'(VALUE "{campo}", ")[^"]*(")', valor, f"{campo} (Runner.rc)")
        # LegalCopyright fica como está: o aviso de copyright do upstream é mantido (AGPL-3.0).
        return t

    def main_cpp(t):
        return troca(t, r'(std::wstring app_name = L")[^"]*(";)', NOME_APP,
                     "nome padrão da janela (runner/main.cpp)")

    def tabbar(t):
        return troca(t, r'(child: const Text\(\s*")[^"]*(",\s*style: TextStyle\(fontSize: 13\),)',
                     NOME_APP, "título fixo na barra de abas (tabbar_widget.dart)")

    def cargo_toml(t):
        ini = t.find("[package.metadata.winres]")
        if ini < 0:
            raise TrechoNaoAchado("[package.metadata.winres] não achado (Cargo.toml)")
        fim = t.find("\n[", ini + 1)
        fim = len(t) if fim < 0 else fim
        bloco = t[ini:fim]
        for campo, valor in (("ProductName", NOME_APP),
                             ("FileDescription", DESCRICAO),
                             ("OriginalFilename", f"{NOME_APP.lower()}.exe")):
            bloco = troca(bloco, rf'(\n{campo} = ")[^"]*(")', valor, f"{campo} (Cargo.toml winres)")
        return t[:ini] + bloco + t[fim:]

    return [
        ("libs/hbb_common/src/config.rs", config_rs),
        ("src/common.rs", common_rs),
        ("flutter/windows/runner/Runner.rc", runner_rc),
        ("flutter/windows/runner/main.cpp", main_cpp),
        ("flutter/lib/desktop/widgets/tabbar_widget.dart", tabbar),
        ("Cargo.toml", cargo_toml),
    ]


# Ícones: arquivo em icones/ -> destinos no checkout. Só copia o que existir em icones/.
ICONES = {
    # Ícone do executável (recurso IDI_APP_ICON do Runner.rc) e do instalador MSI.
    "app_icon.ico": ["flutter/windows/runner/resources/app_icon.ico", "res/icon.ico"],
    # Ícone da bandeja (src/tray.rs usa res/tray-icon.ico se não houver assets/icon.png).
    "tray-icon.ico": ["res/tray-icon.ico"],
    # Cliente "custom": atalhos usam data/flutter_assets/assets/icon.ico (windows.rs get_custom_icon)
    # e a bandeja usa data/flutter_assets/assets/icon.png (tray.rs load_icon_from_asset).
    "icon.ico": ["flutter/assets/icon.ico"],
    "icon.png": ["flutter/assets/icon.png"],
    # Ícone dentro da tela (common.dart loadIcon: icon.png, senão icon.svg).
    "icon.svg": ["flutter/assets/icon.svg"],
    # Logotipo da tela inicial (common.dart _kDefaultLogoAsset), até 300x60.
    "logo.png": ["flutter/assets/logo.png"],
}


def aplica_icones(raiz, pasta_icones, gravar):
    mudou = []
    if not pasta_icones.is_dir():
        print(f"(sem pasta de ícones em {pasta_icones}; ícones do upstream mantidos)")
        return mudou
    for nome, destinos in ICONES.items():
        origem = pasta_icones / nome
        if not origem.is_file():
            continue
        dados = origem.read_bytes()
        for rel in destinos:
            destino = raiz / rel
            if not destino.parent.is_dir():
                raise TrechoNaoAchado(f"pasta de destino do ícone não existe: {rel}")
            if destino.is_file() and destino.read_bytes() == dados:
                continue
            mudou.append(rel)
            if gravar:
                shutil.copyfile(origem, destino)
    return mudou


def main():
    p = argparse.ArgumentParser(description="Aplica a marca IdealAcesso num checkout do RustDesk.")
    p.add_argument("checkout", help="caminho do checkout do rustdesk (com o submódulo libs/hbb_common)")
    p.add_argument("--icones", help="pasta dos ícones (padrão: icones/ ao lado deste script)")
    args = p.parse_args()

    raiz = Path(args.checkout).resolve()
    if not (raiz / "Cargo.toml").is_file() or not (raiz / "libs/hbb_common/src/config.rs").is_file():
        falha(f"{raiz} não parece um checkout do rustdesk com submódulos "
              "(faltam Cargo.toml ou libs/hbb_common/src/config.rs; use --recurse-submodules).")
    pasta_icones = Path(args.icones).resolve() if args.icones else Path(__file__).resolve().parent / "icones"

    chave, servidor, api = le_parametros()

    # 1) calcula tudo em memória; só grava se todos os trechos foram achados.
    novos = {}
    try:
        for rel, fn in edicoes(chave, servidor, api):
            arq = raiz / rel
            if not arq.is_file():
                raise TrechoNaoAchado(f"arquivo não existe: {rel}")
            antigo = arq.read_text(encoding="utf-8")
            novo = fn(antigo)
            if novo != antigo:
                novos[rel] = novo
        icones = aplica_icones(raiz, pasta_icones, gravar=False)
    except TrechoNaoAchado as e:
        falha(f"{e}\nNada foi gravado. Confira o trecho no upstream e ajuste este script.")

    # 2) grava.
    for rel, texto in novos.items():
        with open(raiz / rel, "w", encoding="utf-8", newline="") as f:
            f.write(texto)
    aplica_icones(raiz, pasta_icones, gravar=True)

    if not novos and not icones:
        print(f"Marca {NOME_APP} já aplicada; nada mudou.")
    else:
        for rel in list(novos) + icones:
            print(f"alterado: {rel}")
    print(f"nome={NOME_APP} servidor={servidor} api={api} chave={chave[:6]}...")


if __name__ == "__main__":
    main()
