#!/usr/bin/env python3
"""
Checkout Pix com a Efí (ex-Gerencianet) + verificação automática do pagamento por polling.

Fluxo:
    1. Autentica na API da Efí usando client_id / client_secret + certificado.
    2. Cria uma cobrança Pix imediata (cob) e gera o QR Code (imagem + copia-e-cola).
    3. Fica consultando o status da cobrança de tempos em tempos (polling) até
       ela virar CONCLUIDA (paga), expirar ou dar timeout.

Requisitos:
    pip install efipay
    - Um certificado .pem (ou .p12) baixado no painel da Efí
      (Aplicações -> sua aplicação -> Certificados).
    - Uma chave Pix cadastrada na sua conta Efí.

Credenciais: pegue em https://sejaefi.com.br -> API -> suas aplicações.
Sandbox usa credenciais e certificado do ambiente de HOMOLOGAÇÃO.

⚠️  As credenciais abaixo são SUAS (do recebedor). Você pode deixá-las em
    hardcode aqui pra facilitar, MAS não faça commit deste arquivo com os
    valores reais preenchidos — quem clonar o repo receberia no seu lugar.
    O código lê de variável de ambiente primeiro e cai no hardcode se ela
    não existir, então dá pra usar as duas formas.
"""

import base64
import os
import sys
import time
import uuid

try:
    from efipay import EfiPay
except ImportError:
    sys.exit(
        "Falta a SDK. Instale com:\n\n    pip install efipay\n"
    )


# =========================================================================
# CONFIGURAÇÃO  ->  é AQUI que o token/credencial pode ficar em hardcode.
# Troque os "COLE_..." pelos seus valores, ou defina as variáveis de ambiente
# de mesmo nome (a env var tem prioridade sobre o hardcode).
# =========================================================================
EFI_CLIENT_ID     = os.getenv("EFI_CLIENT_ID",     "COLE_SEU_CLIENT_ID_AQUI")
EFI_CLIENT_SECRET = os.getenv("EFI_CLIENT_SECRET", "COLE_SEU_CLIENT_SECRET_AQUI")
EFI_CERTIFICATE   = os.getenv("EFI_CERTIFICATE",   "./certs/certificado.pem")
EFI_PIX_KEY       = os.getenv("EFI_PIX_KEY",       "COLE_SUA_CHAVE_PIX_AQUI")
EFI_SANDBOX       = os.getenv("EFI_SANDBOX", "true").lower() == "true"

# Polling
POLL_INTERVALO_S  = 5      # segundos entre cada consulta
POLL_TIMEOUT_S    = 15 * 60  # desiste depois disso (deve ser <= expiração da cob)
COB_EXPIRACAO_S   = 15 * 60  # validade da cobrança Pix

# Status possíveis retornados pela Efí para uma cobrança Pix (cob):
STATUS_PAGO       = "CONCLUIDA"
STATUS_ATIVA      = "ATIVA"
STATUS_CANCELADOS = {"REMOVIDA_PELO_USUARIO_RECEBEDOR", "REMOVIDA_PELO_PSP"}


def criar_cliente() -> EfiPay:
    """Instancia o cliente da Efí a partir da configuração acima."""
    faltando = [
        nome for nome, valor in [
            ("EFI_CLIENT_ID", EFI_CLIENT_ID),
            ("EFI_CLIENT_SECRET", EFI_CLIENT_SECRET),
            ("EFI_PIX_KEY", EFI_PIX_KEY),
        ] if not valor or valor.startswith("COLE_")
    ]
    if faltando:
        sys.exit("Configure antes: " + ", ".join(faltando))

    if not os.path.exists(EFI_CERTIFICATE):
        sys.exit(f"Certificado não encontrado em: {EFI_CERTIFICATE}")

    return EfiPay({
        "client_id": EFI_CLIENT_ID,
        "client_secret": EFI_CLIENT_SECRET,
        "sandbox": EFI_SANDBOX,
        "certificate": EFI_CERTIFICATE,
    })


def criar_cobranca(efi: EfiPay, valor: str, nome: str = "", cpf: str = "",
                   descricao: str = "Pagamento") -> dict:
    """
    Cria uma cobrança Pix imediata e devolve os dados úteis do checkout.

    `valor` deve ser string no formato "10.00".
    Retorna dict com: txid, loc_id, copia_e_cola, qrcode_base64.
    """
    body = {
        "calendario": {"expiracao": COB_EXPIRACAO_S},
        "valor": {"original": f"{float(valor):.2f}"},
        "chave": EFI_PIX_KEY,
        "solicitacaoPagador": descricao[:140],
    }
    # devedor é opcional; só envia se veio informado
    if nome and cpf:
        body["devedor"] = {"nome": nome, "cpf": "".join(filter(str.isdigit, cpf))}

    # txid próprio (26-35 chars alfanuméricos) para rastrear o pedido
    txid = uuid.uuid4().hex + uuid.uuid4().hex[:6]  # 38 -> corta abaixo
    txid = txid[:35]

    cob = efi.pix_create_charge(params={"txid": txid}, body=body)
    if "txid" not in cob:
        raise RuntimeError(f"Falha ao criar cobrança: {cob}")

    loc_id = cob["loc"]["id"]
    qr = efi.pix_generate_qrcode(params={"id": loc_id})

    return {
        "txid": cob["txid"],
        "loc_id": loc_id,
        "copia_e_cola": qr["qrcode"],
        "qrcode_base64": qr["imagemQrcode"],  # data URI: data:image/png;base64,...
    }


def salvar_qrcode_png(data_uri: str, caminho: str = "qrcode_pix.png") -> str:
    """Salva a imagem base64 do QR Code em um arquivo PNG."""
    b64 = data_uri.split(",", 1)[-1]
    with open(caminho, "wb") as f:
        f.write(base64.b64decode(b64))
    return caminho


def consultar_status(efi: EfiPay, txid: str) -> str:
    """Consulta o status atual da cobrança."""
    detalhe = efi.pix_detail_charge(params={"txid": txid})
    return detalhe.get("status", "DESCONHECIDO")


def aguardar_pagamento(efi: EfiPay, txid: str) -> bool:
    """
    Faz polling do status até pagar, cancelar ou estourar o timeout.
    Retorna True se foi pago (CONCLUIDA), False caso contrário.
    """
    inicio = time.monotonic()
    print(f"\nAguardando pagamento (consultando a cada {POLL_INTERVALO_S}s)...")
    while True:
        status = consultar_status(efi, txid)
        decorrido = int(time.monotonic() - inicio)
        print(f"  [{decorrido:>4}s] status = {status}")

        if status == STATUS_PAGO:
            print("\n✅ Pagamento CONFIRMADO.")
            return True
        if status in STATUS_CANCELADOS:
            print("\n❌ Cobrança cancelada/removida.")
            return False
        if time.monotonic() - inicio > POLL_TIMEOUT_S:
            print("\n⏱️  Timeout: pagamento não confirmado a tempo.")
            return False

        time.sleep(POLL_INTERVALO_S)


def checkout(valor: str, nome: str = "", cpf: str = "",
             descricao: str = "Pagamento") -> bool:
    """Orquestra o checkout completo. Retorna True se pago."""
    efi = criar_cliente()

    print(f"Criando cobrança Pix de R$ {valor} "
          f"({'SANDBOX' if EFI_SANDBOX else 'PRODUÇÃO'})...")
    dados = criar_cobranca(efi, valor, nome, cpf, descricao)

    png = salvar_qrcode_png(dados["qrcode_base64"])
    print("\n=== CHECKOUT PIX ===")
    print(f"txid........: {dados['txid']}")
    print(f"QR Code.....: {png} (abra a imagem para pagar)")
    print(f"Copia e cola:\n{dados['copia_e_cola']}\n")

    return aguardar_pagamento(efi, dados["txid"])


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Checkout Pix Efí com polling.")
    p.add_argument("valor", help='Valor do pagamento, ex: "10.00"')
    p.add_argument("--nome", default="", help="Nome do pagador (opcional)")
    p.add_argument("--cpf", default="", help="CPF do pagador (opcional)")
    p.add_argument("--desc", default="Pagamento", help="Descrição da cobrança")
    args = p.parse_args()

    pago = checkout(args.valor, args.nome, args.cpf, args.desc)
    sys.exit(0 if pago else 1)
