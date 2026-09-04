# Baixar Mídia Shortcut

Projeto de atalho iOS para salvar mídias públicas compartilhadas por URL sem aplicativo auxiliar no iPhone, sem licença, sem expiração e sem anúncios.

## Plataformas-alvo

YouTube, Instagram, Facebook, X/Twitter, Threads, TikTok, Pinterest, Reddit, Vimeo, Snapchat e Twitch, além de links genéricos quando o extrator conseguir resolver a mídia pública.

## Como funciona

1. Compartilhe um post/vídeo para **Baixar Mídia**; ou copie um link e execute o atalho.
2. O atalho consulta `runtime.json` no GitHub.
3. Na versão 1.0, o runtime usa o serviço open-source `socialdownloader.space` como fallback público.
4. Vídeos e imagens retornados são salvos diretamente no app Fotos.
5. O backend pessoal em `backend/` pode ser implantado em Railway/Render e assumir o processamento depois.

## Por que existe um runtime.json

A URL do backend pode mudar sem exigir que o usuário reinstale ou reimporte um novo atalho assinado.

## Backend próprio

O backend FastAPI usa yt-dlp, gallery-dl, FFmpeg, tratamento específico para Threads, Cobalt opcional, validação anti-SSRF e jobs temporários com limpeza automática.

## Uso

Use somente para conteúdo público que você tenha direito de salvar e respeite os termos e direitos dos criadores/plataformas.
