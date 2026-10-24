interface ImportMetaEnv {
  readonly PUBLIC_TURNSTILE_SITE_KEY?: string;
  readonly PUBLIC_MAX_POR_IP_DIA?: string;
  readonly PUBLIC_MAX_DURACION_S?: string;
  readonly PUBLIC_CODIGO_URL?: string;
  readonly PUBLIC_AUTOR_URL?: string;
  readonly PUBLIC_SITE_URL?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
