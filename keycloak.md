## Configuring Zerto Keycloak Credentials & Swagger API Access

This guide covers **Keycloak OAuth 2.0 / OpenID Connect**, the default
authentication mode in `01_zerto_discover_and_export.py` for Zerto appliances.
Create a dedicated API client for automated access. Legacy deployments supported
by the script use a different session-based flow; see
[authentication modes](seed-assessment.md#usage-and-authentication).

Install dependencies using the [README installation instructions](README.md#installation).
Use `uv run --no-sync` for script execution. Keycloak credentials are used only by
Script 01; Script 02 authenticates to vCenter with `VCENTER_*` settings.

---

### Why Keycloak Authentication is Required for Swagger

When navigating to the built-in Zerto Swagger API documentation (`https://<ZVM_IP>/zvm/docs/`), standard vCenter or administrative user credentials will not authorize API calls.

* **OAuth mode:** Script 01 supplies the returned access token in the HTTP Authorization header (`Authorization: Bearer <token>`) for its `/v1` API requests.
* **Interactive Testing in Swagger:** To execute API calls (`GET`, `POST`, `DELETE`) directly within the Swagger UI, you must first request an access token using your Keycloak Client Credentials, then paste that token into Swagger's **Authorize** modal.
* **Non-Interactive Automation:** Python scripts (such as `01_zerto_discover_and_export.py`) use the same Client ID and Secret to request short-lived bearer tokens programmatically without human intervention.

---

### Step-by-Step: Creating an API Client in Zerto Keycloak

The screenshots show one Keycloak UI version. Labels vary: newer versions may
use **Client authentication** and **Service accounts roles** instead of
**Access Type: confidential** and **Service Accounts Enabled**. Confirm the
equivalent settings and service-account permissions for the deployed version.

1. **Access the Keycloak Administration Console**
* Open your browser and navigate to:
```text
https://<ZVM_IP>/auth/admin/

```

* Log in using your ZVMA system administrator credentials.

![alt text](images/keycloak-01.png)

2. **Select the Zerto Realm**
* Ensure you are in the **`zerto`** realm (selectable from the top-left realm dropdown menu).

![alt text](images/keycloak-02.png)

3. **Create a New OAuth Client**
* In the left navigation panel, click **Clients**.
* Click **Create** in the top-right corner.
* Configure the following basic parameters:
* **Client ID:** `zerto-python-script` (or a descriptive name of your choice)
* **Client Protocol:** `openid-connect`

![alt text](images/keycloak-03.png)

* Click **Save**.


4. **Configure Access Settings**
* On the **Settings** tab for your new client, update the following fields:
* **Access Type:** Set to `confidential`
* **Service Accounts Enabled:** Toggle to `ON` (This enables the `client_credentials` grant type required for headless script authentication)
* **Direct Access Grants Enabled:** Toggle to `OFF`

![alt text](images/keycloak-04.png)
![alt text](images/keycloak-05.png)
* Click **Save**.


5. **Retrieve the Client Secret**
* Click the **Credentials** tab at the top of the client configuration screen.
* Locate the **Secret** field. Copy this value and store it securely.

![alt text](images/keycloak-06.png)

---

### Configure Script 01

Set the following in the git-ignored `.env` file or inject them through your
approved secret manager:

```dotenv
ZVM_IP=your.zvm.ip.or.fqdn
ZVM_AUTH_MODE=oauth
ZVM_CLIENT_ID=your-api-client
ZVM_CLIENT_SECRET=replace-with-your-client-secret
VERIFY_SSL=true
```

Use `VERIFY_SSL=true` when the appliance certificate is trusted. The repository
default is `false`; establish certificate trust rather than assuming verification
is enabled. Do not commit populated credentials.

```bash
uv run --no-sync 01_zerto_discover_and_export.py
```

The script requests an access token automatically; no manual token copy is needed.
This command also queries the appliance and writes the discovery/assessment JSON,
so run it while protection is active, before unprotecting migration workloads.
Use `--help` instead to inspect options without connecting.

### Optional: Testing Credentials in Swagger UI

1. Open the deployed Zerto Swagger UI (commonly `https://<ZVM_IP>/zvm/docs/`).
2. Obtain an OAuth access token through your approved API client or the UI's
   supported OAuth flow. Script 01 uses
   `POST /auth/realms/zerto/protocol/openid-connect/token` with
   `grant_type=client_credentials`, `client_id`, and `client_secret` as form data.
   This token endpoint may not be listed in the Zerto `/v1` Swagger document.
3. Use **Authorize** and follow the security scheme displayed by that Swagger
   version: some bearer fields expect only the token, while others expect
   `Bearer <token>`. Avoid adding the prefix twice.
4. Confirm the service account has permission for the intended requests.

Keep tokens and client secrets out of logs, screenshots, generated documentation,
and version control. Keycloak administration and Swagger access are separate
from vCenter authentication.
