import type Keycloak from "keycloak-js";

const keycloakConfig = {
  url: process.env.NEXT_PUBLIC_KEYCLOAK_URL || "https://iam.dev.sandbox.internal.nbank.fr/auth",
  realm: process.env.NEXT_PUBLIC_KEYCLOAK_REALM || "internal",
  clientId: process.env.NEXT_PUBLIC_KEYCLOAK_CLIENT_ID || "web-app",
};

let starting: Promise<Keycloak> | null = null;

export function startKeycloak(): Promise<Keycloak> {
  if (!starting) {
    starting = import("keycloak-js").then(async ({ default: KeycloakClient }) => {
      const client = new KeycloakClient(keycloakConfig);
      await client.init({ onLoad: "login-required", pkceMethod: "S256", checkLoginIframe: false });
      return client;
    });
  }
  return starting;
}

export async function login(): Promise<void> {
  const client = await startKeycloak();
  await client.login();
}

export async function logout(): Promise<void> {
  const client = await startKeycloak();
  if (client.authenticated) await client.logout({ redirectUri: window.location.origin });
}
