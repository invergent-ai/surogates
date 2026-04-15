{{/*
Fullname — the release name, which is the agent slug.
*/}}
{{- define "surogates.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Chart label value.
*/}}
{{- define "surogates.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "surogates.labels" -}}
app: surogates
agent: {{ .Values.agent.slug }}
helm.sh/chart: {{ include "surogates.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Selector labels — the minimal set used in Deployment.spec.selector.matchLabels.
*/}}
{{- define "surogates.selectorLabels" -}}
app: surogates
agent: {{ .Values.agent.slug }}
{{- end }}

{{/*
Component labels — extends selector labels with a component name.
Usage: include "surogates.componentLabels" (dict "root" . "component" "api")
*/}}
{{- define "surogates.componentLabels" -}}
{{ include "surogates.selectorLabels" .root }}
component: {{ .component }}
{{- end }}

{{/*
Component selector — for Service selectors and Deployment matchLabels.
Usage: include "surogates.componentSelector" (dict "root" . "component" "api")
*/}}
{{- define "surogates.componentSelector" -}}
{{ include "surogates.componentLabels" . }}
{{- end }}

{{/*
Full labels with component — common labels + component.
Usage: include "surogates.fullLabels" (dict "root" . "component" "api")
*/}}
{{- define "surogates.fullLabels" -}}
{{ include "surogates.labels" .root }}
component: {{ .component }}
{{- end }}

{{/*
Agent hostname — {slug}.{domain}.
*/}}
{{- define "surogates.hostname" -}}
{{- printf "%s.%s" .Values.agent.slug .Values.agent.domain }}
{{- end }}

{{/*
Internal API service URL — used by worker to reach the API server.
*/}}
{{- define "surogates.apiServiceUrl" -}}
{{- printf "http://%s-api.%s.svc:8000" (include "surogates.fullname" .) .Release.Namespace }}
{{- end }}

{{/*
Internal MCP proxy service URL.
*/}}
{{- define "surogates.mcpProxyUrl" -}}
{{- printf "http://%s-mcp-proxy.%s.svc:8001" (include "surogates.fullname" .) .Release.Namespace }}
{{- end }}

{{/*
Image reference — resolves {repository}:{tag} for a named image.

Canonical defaults are baked into the chart; values.yaml entries are
optional overrides (either full or partial: override just `tag` or just
`repository`). Known names: "api", "worker", "s3fs".

Usage: include "surogates.image" (dict "root" . "name" "api")
*/}}
{{- define "surogates.image" -}}
{{- $defaults := dict
  "api"    (dict "repository" "ghcr.io/invergent-ai/surogates-api"    "tag" "latest")
  "worker" (dict "repository" "ghcr.io/invergent-ai/surogates-worker" "tag" "latest")
  "s3fs"   (dict "repository" "ghcr.io/invergent-ai/surogates-s3fs"   "tag" "latest")
-}}
{{- $default := index $defaults .name -}}
{{- if not $default -}}
  {{- fail (printf "surogates.image: unknown image name %q" .name) -}}
{{- end -}}
{{- $images := default (dict) .root.Values.images -}}
{{- $override := default (dict) (index $images .name) -}}
{{- $repo := default $default.repository $override.repository -}}
{{- $tag := default $default.tag $override.tag -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end }}

{{/*
Image pull policy — `.Values.images.pullPolicy` with IfNotPresent default.
*/}}
{{- define "surogates.imagePullPolicy" -}}
{{- $images := default (dict) .Values.images -}}
{{- default "IfNotPresent" $images.pullPolicy -}}
{{- end }}
