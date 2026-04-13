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
