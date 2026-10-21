DOMAIN = "ica_shopping"
DATA_ICA = f"{DOMAIN}._data"

CONF_AUTH = "auth"
CONF_LIST_ID = "ica_list_id"
CONF_TODO_ENTITY = "todo_entity_id"
CONF_REMOVE_STRIKED = "remove_striked"
CONF_NEVER_ADD = "never_add"
CONF_CALENDAR = "calendar_entity_id"

DEFAULT_NEVER_ADD = ["salt", "peppar", "svartpeppar", "vatten", "olja", "olivolja", "smör"]
CONF_STORE_ID = "store_id"

# ICA rejects more rows per list; checked rows count too.
MAX_ICA_ITEMS = 250

IMS_BASE = "https://ims.icagruppen.se"
API_BASE = "https://apimgw-pub.ica.se"

TOKEN_ENDPOINT = f"{IMS_BASE}/oauth/v2/token"
AUTHORIZE_ENDPOINT = f"{IMS_BASE}/oauth/v2/authorize"
REGISTER_ENDPOINT = f"{IMS_BASE}/register"
LOGIN_ENDPOINT = f"{IMS_BASE}/authn/authenticate/IcaCustomers"

# Public constants shipped in ICA's mobile app (via LazyTarget/ha-ica-todo).
DCR_CLIENT_ID = "ica-app-dcr-registration"
DCR_CLIENT_SECRET = "uxLHTBvZ-Z2fV-SbrHl1E-tz7vB3jQFrwAdSLlbVMMu1rxDdvJU0s8KGu9d1wLS4"
DCR_SOFTWARE_ID = "dcr-ica-app-template"
REDIRECT_URI = "icacurity://app"
ACR = "urn:se:curity:authentication:html-form:IcaCustomers"

SL_PATH = "sverige/digx/mobile/shoppinglistservice/v1/shoppinglists"
# ICA's names for groceries, each with the aisle category the app sorts the list by.
ARTICLES_PATH = "sverige/digx/mobile/shoppinglistservice/v1/articles"
RECIPE_PATH = "sverige/digx/mobile/recipeservice/v1"
STORE_PATH = "sverige/digx/mobile/storeservice/v1"
OFFER_PATH = "sverige/digx/mobile/offerservice/v1"
BONUS_PATH = "sverige/digx/mobile/bonusservice/v1"

# ica.se's recipe search; it takes the anonymous token ica.se hands every visitor.
RECIPE_SEARCH_URL = "https://apim-pub.gw.ica.se/sverige/digx/mdsarecipesearch/v1"
# ica.se's site search (products among other things) and store search, on the same token.
GLOBAL_SEARCH_URL = "https://apim-pub.gw.ica.se/sverige/digx/globalsearch/v1"
STORE_SEARCH_URL = "https://apim-pub.gw.ica.se/sverige/digx/storesearch/v1"
PUBLIC_TOKEN_URL = "https://www.ica.se/e11/public-access-token"
RECIPE_IMAGE_BASE = "https://assets.icanet.se"
# Public recipe pages; their page data has the tags planning uses to tell what a dish is built around.
RECIPE_WEB_URL = "https://www.ica.se/recept"

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)
