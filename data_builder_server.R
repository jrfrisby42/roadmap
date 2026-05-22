# =============================================================================
# data_builder_server.R  — server-safe R data builder
# Self-contained: sources /opt/pm/r/db.R and /opt/pm/r/helpers.R
# Resource changes vs original:
#   1. CPM loaded in 3 x 12-month SQL-filtered chunks (not cpm_table() all at once)
#   2. Sales filtered at SQL level (CASE only, last 14 months)
#   3. Installations filtered at SQL level (active only)
#   4. doParallel removed — sequential lapply/rollapply throughout
#   5. gc() after every heavy operation
#   6. Writes dashboard_cache.json for Python dashboard
# Logic: identical to original data_builder.R
# =============================================================================

source("/opt/pm/r/db.R")
source("/opt/pm/r/helpers.R")

library(jsonlite)
library(RColorBrewer)

cat(sprintf("[%s] === data_builder_server.R starting ===\n",
            format(Sys.time(), "%H:%M:%S")))

# Throttle CPU — use at most 2 threads for data.table operations
data.table::setDTthreads(2)

# Lower R's own process priority (belt-and-suspenders alongside nice/ionice)
# This keeps the server responsive to web requests during the build
try(tools::pskill(Sys.getpid(), signal = 0L), silent = TRUE)  # no-op, just a check

# Load .env from /opt/pm/
dotenv::load_dot_env("/opt/pm/.env")

# ── Date helpers ──────────────────────────────────────────────────────────────
today_dt <- today()

# Exact logic from MDFF.R:
#   ttt  = floor_date(today, "month") - 1mo  (if day>=10)
#          floor_date(today, "month") - 2mo  (if day<10, data not loaded yet)
#   tttc = floor_date(today, "month")        -- FIRST OF CURRENT MONTH
#   lttm = ttt - 11 months
ttt <- if (day(today_dt) < 10) {
  floor_date(today_dt, "month") %m-% months(2)
} else {
  floor_date(today_dt, "month") %m-% months(1)
}
tttc  <- floor_date(today_dt, "month")   # first of current month
lttm  <- ttt - months(11)
ttt_1 <- ttt - months(1)
cat(sprintf("[%s] ttt=%s  tttc=%s  lttm=%s\n",
            format(Sys.time(), "%H:%M:%S"), ttt, tttc, lttm))

# ── Helper: open one connection, run query, close ────────────────────────────
dbq <- function(sql) {
  con <- get_db_con()
  on.exit(DBI::dbDisconnect(con))
  DBI::dbGetQuery(con, sql)
}

# ── Helper: load full table by prefixed name ──────────────────────────────────
dbl <- function(name, prefix = "fraznetapp_") {
  cat(sprintf("[%s]   Loading %s%s ...\n", format(Sys.time(),"%H:%M:%S"), prefix, name))
  dbq(sprintf("SELECT * FROM `%s%s`", prefix, name))
}

# ── Load small reference tables ───────────────────────────────────────────────
cat(sprintf("[%s] Loading reference tables...\n", format(Sys.time(),"%H:%M:%S")))
stores        <- dbl("stores")
dists         <- dbl("distributors")
chains        <- dbl("chains")
channels      <- dbl("channel")
machs         <- dbl("machines")
mw            <- dbl("machinewatch")
wstat         <- dbl("machinewatchstatus")
lj            <- dbl("logisticsjob")
cj            <- dbl("closedjob")
joblog        <- dbq("SELECT job_id, details FROM `fraznetapp_joblog`")
sales_regions <- dbl("salesregions")
rem           <- dbl("remediationplan")
atrisk        <- dbl("atrisk")
sdeal         <- dbq("SELECT * FROM `sales_deal`")
ljtype        <- dbl("logisticsjobtype")
exstat        <- dbl("externalstatus")
hspipe_stage  <- dbq("SELECT * FROM `hsintegration_hspipelinestage`")
users         <- dbq("SELECT * FROM `auth_user`")

# PM mapping
pm_map <- dbl("performancemanagement") %>%
  left_join(users %>% select(user_id = id, pm = first_name), "user_id") %>%
  select(pm_id = id, pm)
pm_dist_map1 <- dists %>%
  inner_join(pm_map, "pm_id") %>%
  select(dist = name, pm)

cat(sprintf("[%s] Reference tables loaded.\n", format(Sys.time(),"%H:%M:%S")))

# ── Sales — join sales + products, filter to CASE purchases, last 14 months ──
# sales_table() in original = fraznetapp_sales JOIN fraznetapp_products
# giving columns: store_id_id, date, qty, item (product name), category
cat(sprintf("[%s] Loading sales (filtered)...\n", format(Sys.time(),"%H:%M:%S")))
sales_cutoff <- format(ttt - months(14), "%Y-%m-%d")
sales <- dbq(sprintf(
  "SELECT s.store_id_id,
          DATE_FORMAT(s.date, '%%Y-%%m-01') AS date,
          s.quantity                         AS qty,
          p.name                             AS item,
          p.category                         AS category
   FROM   `fraznetapp_sales`    s
   JOIN   `fraznetapp_products` p ON p.id  = s.product_id_id
   WHERE  p.category = 'CASE'
     AND  s.quantity > 0
     AND  s.date     >= '%s'
     AND  s.store_id_id IS NOT NULL
     AND  p.name        IS NOT NULL",
  sales_cutoff))
sales$date <- as.Date(sales$date)
cat(sprintf("[%s]   sales rows: %d\n", format(Sys.time(),"%H:%M:%S"), nrow(sales)))
gc()

# ── Installations — SQL-filtered: active machines only ────────────────────────
cat(sprintf("[%s] Loading installations (filtered)...\n", format(Sys.time(),"%H:%M:%S")))
inst_cutoff <- format(ttt + months(1), "%Y-%m-%d")
inst <- dbq(sprintf(
  "SELECT store_id_id, machine_id_id, date, moved_date
   FROM `fraznetapp_installations`
   WHERE date < '%s' AND (moved_date IS NULL OR moved_date >= '%s')",
  inst_cutoff, inst_cutoff))
inst$date       <- as.Date(inst$date)
inst$moved_date <- as.Date(inst$moved_date)
gc()

# ── CPM table — 3 x 12-month SQL chunks ───────────────────────────────────────
cat(sprintf("[%s] Loading CPM (3 x 12-month chunks)...\n", format(Sys.time(),"%H:%M:%S")))

load_cpm_chunk <- function(d_start, d_end) {
  cat(sprintf("[%s]   Chunk %s -> %s\n",
              format(Sys.time(),"%H:%M:%S"),
              format(d_start,"%Y-%m-%d"), format(d_end,"%Y-%m-%d")))
  dbq(sprintf(
    "SELECT * FROM `fraznetapp_cpm`
     WHERE date >= '%s' AND date <= '%s'",
    format(d_start,"%Y-%m-%d"), format(d_end,"%Y-%m-%d")))
}

chunk1 <- load_cpm_chunk(ttt - months(11), ttt)
gc(); Sys.sleep(2)
chunk2 <- load_cpm_chunk(ttt - months(23), ttt - months(12))
gc(); Sys.sleep(2)
chunk3 <- load_cpm_chunk(ttt - months(35), ttt - months(24))
gc(); Sys.sleep(2)

cpmt <- bind_rows(chunk1, chunk2, chunk3)
rm(chunk1, chunk2, chunk3); gc()
cat(sprintf("[%s] CPM loaded: %d rows\n", format(Sys.time(),"%H:%M:%S"), nrow(cpmt)))

# ── Build cc0 (replaces cpm_table(TRUE)) ─────────────────────────────────────
cat(sprintf("[%s] Building cc0...\n", format(Sys.time(),"%H:%M:%S")))

# Rename CPM columns to match original cpm_table() output
# fraznetapp_cpm uses store_id (not store_id_id), and may vary — rename only if present
cpmt <- cpmt %>%
  rename(any_of(c(
    mfac     = "monthly_factor",
    store_id = "store_id_id"      # rename only if this column exists
  ))) %>%
  mutate(st_date = floor_date(as.Date(current_account_start_date), "month")) %>%
  left_join(dists  %>% select(distributor_id = id, dist = name), "distributor_id") %>%
  left_join(chains %>% select(chain_id = id, chain = name),       "chain_id") %>%
  left_join(stores %>% select(store_id = id, store_name = name,
                              street, city, state, zip), "store_id")

cpmt$date    <- as.Date(cpmt$date)
cpmt$st_date <- as.Date(cpmt$st_date)

# Filter to relevant channels
valid_stores <- stores %>%
  filter(channel_id %in% (channels %>%
           filter(name %in% c("C-Store","Grocery","College & University","Liquor")) %>%
           pull(id))) %>%
  pull(id)

cc0 <- cpmt %>%
  left_join(pm_dist_map1, "dist") %>%
  filter(store_id %in% valid_stores) %>%
  arrange(store_id, date) %>%
  select(any_of(c("store_id", "store_name", "dist", "chain", "date",
                  "external_store_id", "st_date", "data", "program",
                  "mfac", "machines", "factored_machines", "cases",
                  "affiliate_id", "street", "city", "state", "zip")), pm)

rm(cpmt); gc()
cat(sprintf("[%s] cc0: %d rows\n", format(Sys.time(),"%H:%M:%S"), nrow(cc0)))

# ── Machine age ───────────────────────────────────────────────────────────────
cat(sprintf("[%s] Machine age...\n", format(Sys.time(),"%H:%M:%S")))
machs_age <- inst %>%
  left_join(machs %>% select(id, bunn_serial_number),
            by = c("machine_id_id" = "id")) %>%
  select(store_id_id, machine_id_id, bunn_serial_number) %>%
  rename(serial_no = bunn_serial_number) %>%
  mutate(
    x = gsub("ULTR([0-9]{2}).*", "\\1", serial_no),
    vyear = case_when(
      slsp("00|01",x) ~ as.Date("2003-01-01"), slsp("02",x) ~ as.Date("2004-01-01"),
      slsp("03",x)    ~ as.Date("2005-01-01"), slsp("04",x) ~ as.Date("2006-01-01"),
      slsp("05",x)    ~ as.Date("2007-01-01"), slsp("06|07",x) ~ as.Date("2008-01-01"),
      slsp("08|09",x) ~ as.Date("2009-01-01"), slsp("10",x) ~ as.Date("2010-01-01"),
      slsp("11|12|13",x) ~ as.Date("2011-01-01"), slsp("14|15",x) ~ as.Date("2012-01-01"),
      slsp("16",x) ~ as.Date("2013-01-01"),    slsp("17",x) ~ as.Date("2014-01-01"),
      slsp("18|19",x) ~ as.Date("2015-01-01"), slsp("20",x) ~ as.Date("2016-01-01"),
      slsp("21|22|23",x) ~ as.Date("2017-01-01"), slsp("24",x) ~ as.Date("2018-01-01"),
      slsp("25|26",x) ~ as.Date("2019-01-01"), slsp("27",x) ~ as.Date("2020-01-01"),
      !is.na(x) ~ as.Date("2021-01-01"))) %>%
  distinct(store_id_id, machine_id_id, .keep_all = TRUE) %>%
  mutate(vage = as.numeric(difftime(today(), vyear, units = "days")) / 365.25)

# ── PM Categories ─────────────────────────────────────────────────────────────
cat(sprintf("[%s] PM categories...\n", format(Sys.time(),"%H:%M:%S")))
machs_wstat1 <- mw %>%
  left_join(wstat %>% select(watch_status_id = id, watch_status = status),
            "watch_status_id") %>%
  select(machine_id, recovery_status, watch_status)

ref_date <- if (any(cc0$date == tttc)) tttc else ttt
machs_pm_cat <- cc0 %>%
  filter(date == ref_date) %>%
  select(store_id, chain) %>%
  left_join(stores %>% select(store_id = id, aff_id = affiliate_group_id), "store_id") %>%
  left_join(chains %>% select(chain = name, rank_202), "chain") %>%
  left_join(machs_age %>% select(store_id = store_id_id,
                                  machine_id = machine_id_id, vage),
            "store_id", relationship = "many-to-many") %>%
  left_join(inst %>%
              filter(is.na(moved_date)) %>%
              distinct(store_id_id, machine_id_id, .keep_all = TRUE) %>%
              inner_join(machs_wstat1, c("machine_id_id" = "machine_id")) %>%
              select(machine_id = machine_id_id, recovery_status, watch_status),
            "machine_id") %>%
  mutate(
    pm_cat  = case_when(!is.na(rank_202) | !is.na(aff_id) ~ "Top 202",
                        .default = "Addressable"),
    pm_cat2 = case_when(vage >= 15 & !is.na(watch_status) ~ "Old Machine, Sleuthing",
                        vage >= 15 ~ "Old Machine",
                        !is.na(watch_status) ~ "Sleuthing",
                        .default = "Standard"))

stores_pm_cat <- machs_pm_cat %>%
  group_by(store_id) %>%
  summarise(
    pm_cat  = case_when(any(slsp("top 202", pm_cat)) ~ "Top 202", .default = "Addressable"),
    pm_cat2 = case_when(
      any(slsp("old machine", pm_cat2)) & any(slsp("sleuth", pm_cat2)) ~ "Old Machine, Sleuthing",
      any(slsp("old machine", pm_cat2)) ~ "Old Machine",
      any(slsp("sleuth", pm_cat2)) ~ "Sleuthing",
      .default = "Standard")) %>%
  ungroup()

# ── Build cc ──────────────────────────────────────────────────────────────────
cat(sprintf("[%s] Building cc...\n", format(Sys.time(),"%H:%M:%S")))
cc <- cc0 %>%
  left_join(stores_pm_cat, "store_id") %>%
  uncount(2) %>%
  group_by(store_id, date) %>%
  mutate(pm = ifelse(row_number() == 2, "total", pm)) %>%
  ungroup() %>%
  mutate(
    # Coerce data column to integer (MySQL may return as character "1"/"0")
    data    = as.integer(as.character(data)),
    pm_cat  = factor(pm_cat,  levels = c("Addressable","Top 202")),
    pm_cat2 = factor(pm_cat2, levels = c("Standard","Sleuthing","Old Machine","Old Machine, Sleuthing"))) %>%
  arrange(pm, store_id, date)

rm(cc0); gc()
cat(sprintf("[%s] cc: %d rows\n", format(Sys.time(),"%H:%M:%S"), nrow(cc)))

# Pre-build cc with Global rows added ONCE — reused by all sections
# This avoids repeating group_modify(bind_rows(pm_cat="Global")) everywhere
cat(sprintf("[%s] Building cc_global (add Global pm_cat rows)...\n", format(Sys.time(),"%H:%M:%S")))
cc_global <- bind_rows(cc, cc %>% mutate(pm_cat = "Global"))
gc()
cat(sprintf("[%s] cc_global: %d rows\n", format(Sys.time(),"%H:%M:%S"), nrow(cc_global)))

# Pre-compute last purchase per store from sales (used by NMNO, XMNO, Goals)
last_purch_by_store <- sales %>%
  filter(category == "CASE", qty > 0, !is.na(item)) %>%
  group_by(store_id_id) %>%
  summarise(lastpurch = max(date), .groups = "drop")

# =============================================================================
# From here: IDENTICAL logic to original data_builder.R
# =============================================================================

# NMNO -------------------------------------------------------------------------
cat(sprintf("[%s] NMNO...\n", format(Sys.time(),"%H:%M:%S")))
nmno_df <- cc %>%
  select(pm, store_id, date, chain, machines, cases, st_date, program, data) %>%
  arrange(store_id, date) %>%
  group_by(store_id) %>%
  mutate(first_date = min(date)) %>%
  filter(any(machines > 0),
         any(slsp("bundle", program)),
         case_when(any(date == st_date) ~ any(program[date == st_date] == "BUNDLE"), TRUE ~ TRUE)) %>%
  left_join(last_purch_by_store, c("store_id" = "store_id_id")) %>%
  ungroup() %>%
  filter(case_when(is.na(lastpurch) ~ TRUE,
                   lastpurch < first_date ~ lastpurch < st_date - months(2),
                   TRUE ~ TRUE)) %>%
  group_by(store_id) %>%
  filter(date >= st_date - months(2)) %>%
  mutate(ctq = cumsum(cases)) %>%
  ungroup() %>%
  mutate(nmno = interval(st_date, date) / months(1) + 1) %>%
  filter(nmno >= 2) %>%
  group_by(store_id) %>%
  filter(ctq == 0 | lag(ctq) == 0, date >= ttt - months(1)) %>%
  filter(any(slsp("bundle", program) & machines > 0),
         all(data[date >= st_date] == 1)) %>%
  ungroup() %>%
  filter(st_date <= date - months(1), nmno >= 2) %>%
  filter(date >= ttt - months(1)) %>%
  group_by(pm, store_id) %>%
  filter(any(date == ttt)) %>%
  left_join(stores_pm_cat, "store_id") %>%
  mutate(
    is_nmno   = ctq <= 0 & machines > 0,
    nmno_bins = cut(nmno, c(2:12, Inf), 2:12, right = FALSE),
    pm_cat    = factor(pm_cat,  levels = c("Addressable","Top 202")),
    pm_cat2   = factor(pm_cat2, levels = c("Standard","Sleuthing","Old Machine","Old Machine, Sleuthing"))) %>%
  reframe(
    ncat      = case_when(any(date == ttt & nmno == 2 & is_nmno) ~ "New",
                          all(is_nmno) ~ "Stayed",
                          any(!is_nmno & date >= ttt) ~ "Left"),
    nmno      = nmno[date == ttt],
    nmno_bins = nmno_bins[date == ttt],
    pm_cat    = pm_cat[date == ttt],
    pm_cat2   = pm_cat2[date == ttt],
    machines  = machines[date == ttt]) %>%
  ungroup() %>%
  mutate(uid = row_number()) %>%
  uncount(2) %>%
  group_by(uid) %>%
  mutate(pm     = case_when(row_number() == 2 ~ "total", TRUE ~ pm),
         pm_cat = factor(pm_cat,  levels = c("Addressable","Top 202")),
         pm_cat2= factor(pm_cat2, levels = c("Standard","Sleuthing","Old Machine","Old Machine, Sleuthing"))) %>%
  ungroup() %>%
  select(-uid) %>%
  group_modify(~{ .x %>% bind_rows(.x %>% mutate(pm_cat = "Global")) })
gc()
cat(sprintf("[%s] NMNO: %d rows\n", format(Sys.time(),"%H:%M:%S"), nrow(nmno_df)))
Sys.sleep(3)

# CPM Histogram ----------------------------------------------------------------
cat(sprintf("[%s] CPM Histogram...\n", format(Sys.time(),"%H:%M:%S")))
# Build t_ann_cpm: compute per pm/pm_cat first, then add Global separately
ann_base <- cc %>%
  arrange(store_id, date) %>%
  filter(slsp("bundle|pending|demo", program),
         date >= ttt - months(11), date <= ttt, data == 1, !is.na(pm)) %>%
  group_by(pm, pm_cat) %>%
  summarise(cases = sum(cases), factored_machines = sum(factored_machines), .groups = "drop") %>%
  mutate(cpm = round(cases / factored_machines, 1))

ann_global <- ann_base %>%
  filter(pm_cat != "Global") %>%
  group_by(pm) %>%
  summarise(cases = sum(cases), factored_machines = sum(factored_machines), .groups = "drop") %>%
  mutate(cpm = round(cases / factored_machines, 1), pm_cat = "Global")

t_ann_cpm <- bind_rows(ann_base, ann_global) %>%
  select(pm, pm_cat, cpm)

active_stores <- cc %>%
  filter(slsp("bundle|pending|demo", program),
         date >= ttt - months(11), date <= ttt, data == 1, !is.na(pm)) %>%
  group_by(store_id, pm) %>%
  filter(any(date == ttt & slsp("bundle", program))) %>%
  ungroup()

act_base <- active_stores %>%
  group_by(pm, pm_cat) %>%
  summarise(cases = sum(cases), factored_machines = sum(factored_machines), .groups = "drop") %>%
  mutate(cpm = round(cases / factored_machines, 1))

act_global <- act_base %>%
  filter(pm_cat != "Global") %>%
  group_by(pm) %>%
  summarise(cases = sum(cases), factored_machines = sum(factored_machines), .groups = "drop") %>%
  mutate(cpm = round(cases / factored_machines, 1), pm_cat = "Global")

t_active_cpm <- bind_rows(act_base, act_global) %>%
  select(pm, pm_cat, cpm)

rm(active_stores, act_base, act_global)

cpm_hist_data <- cc %>%
  arrange(store_id, date) %>%
  filter(slsp("bundle|pending|demo", program),
         date >= ttt - months(11), date <= ttt, data == 1, !is.na(pm)) %>%
  group_by(store_id, pm) %>%
  reframe(
    dist      = last(dist),
    chain     = last(chain),
    st_date   = st_date[1],
    program   = last(program),
    machines  = machines[date == ttt],
    ltm_cases = sum(cases),
    fmach     = sum(factored_machines),
    cpm       = ltm_cases / fmach) %>%
  group_by(store_id, pm) %>%
  mutate(
    cpm_bins    = cut(cpm, breaks = c(-Inf, seq(2,40,2), 50, 60, Inf),
                      labels = c(0, seq(2,40,2), 50, 60), right = FALSE, include.lowest = TRUE),
    perf_groups = cut(cpm, breaks = c(-Inf, 2, 12, 20, 30, Inf),
                      labels = c("Non-Purchasing","At Risk","Under Performing",
                                 "Compliant","Additional Machine Qualified"),
                      right = FALSE, include.lowest = TRUE),
    perf_groups = case_when(st_date > ttt - months(2) ~ "<3 Months Old", .default = perf_groups),
    perf_groups = factor(perf_groups,
                         levels = c("Non-Purchasing","At Risk","Under Performing",
                                    "Compliant","Additional Machine Qualified","<3 Months Old"))) %>%
  mutate(elig = slsp("bundle", program), .before = program) %>%
  left_join(stores_pm_cat, "store_id") %>%
  mutate(
    pm_cat  = factor(pm_cat,  levels = c("Addressable","Top 202")),
    pm_cat2 = factor(pm_cat2, levels = c("Standard","Sleuthing","Old Machine","Old Machine, Sleuthing")))

cpm_hist_summary <- cpm_hist_data %>%
  ungroup() %>%
  bind_rows(cpm_hist_data %>% ungroup() %>% mutate(pm_cat = "Global")) %>%
  arrange(store_id, pm_cat) %>%
  filter(machines > 0) %>%
  group_by(pm, pm_cat) %>%
  mutate(cpm = sum(ltm_cases)/sum(fmach)) %>%
  group_by(pm, pm_cat, perf_groups) %>%
  summarise(cpm = cpm[1], `# of Stores` = sum(elig), Machines = sum(machines[elig]), .groups = "drop") %>%
  mutate(`% of all Stores` = `# of Stores`/sum(`# of Stores`)) %>%
  complete(perf_groups = c("Non-Purchasing","At Risk","Under Performing",
                           "Compliant","Additional Machine Qualified","<3 Months Old")) %>%
  mutate(across(everything(), .fns = ~replace(., is.na(.), 0)))

cpm_hist_data1 <- cpm_hist_data %>%
  ungroup() %>%
  bind_rows(cpm_hist_data %>% ungroup() %>% mutate(pm_cat = "Global")) %>%
  left_join(cpm_hist_summary %>%
              rename(pg_cpm = cpm) %>%
              mutate(label1 = paste0(perf_groups, "\n# of Stores: ", `# of Stores`, "\n",
                                     "% of all Stores: ", percent(`% of all Stores`, 0.1),
                                     "\nMachines: ", Machines)),
            c("pm","pm_cat","perf_groups")) %>%
  filter(machines > 0, elig) %>%
  group_by(pm, pm_cat, cpm_bins, perf_groups) %>%
  summarise(label1 = label1[1], y = n(), .groups = "drop") %>%
  mutate(
    y           = ifelse(slsp("3 month", perf_groups), 0, y),
    cpm_bins    = ifelse(slsp("3 month", perf_groups), "-1", as.character(cpm_bins)),
    cpm_bins    = factor(cpm_bins, levels = c(-1, seq(0,40,2), 50, 60)),
    perf_groups = factor(perf_groups, levels = c("Non-Purchasing","At Risk","Under Performing",
                                                  "Compliant","Additional Machine Qualified","<3 Months Old")),
    vv          = ifelse(slsp("3 month", perf_groups), "legendonly", "TRUE")) %>%
  arrange(pm, match(pm_cat, levels(pm_cat)), match(perf_groups, levels(perf_groups)))

gc()
cat(sprintf("[%s] CPM Histogram done.\n", format(Sys.time(),"%H:%M:%S")))
Sys.sleep(3)

# CPM History — sequential lapply (was doParallel 10 cores) -------------------
cat(sprintf("[%s] CPM History (sequential)...\n", format(Sys.time(),"%H:%M:%S")))
cpm_history <- lapply(seq(lttm, ttt, "1 month"), function(i) {
  w <- cc %>%
    filter(slsp("bundle|pending|demo", program),
           date >= i - months(11), date <= i, data == 1, !is.na(pm))

  ann_b <- w %>% group_by(pm, pm_cat) %>%
    summarise(cases = sum(cases), fmach = sum(factored_machines), .groups = "drop") %>%
    mutate(cpm = round(cases/fmach, 1))
  ann_g <- ann_b %>% filter(pm_cat != "Global") %>% group_by(pm) %>%
    summarise(cases = sum(cases), fmach = sum(fmach), .groups = "drop") %>%
    mutate(cpm = round(cases/fmach, 1), pm_cat = "Global")
  ann <- bind_rows(ann_b, ann_g) %>%
    select(pm, pm_cat, cpm) %>% mutate(met = "Annualized", date = i)

  act_sids <- w %>% group_by(store_id, pm) %>%
    filter(any(date == i & slsp("bundle", program))) %>% ungroup()
  act_b <- act_sids %>% group_by(pm, pm_cat) %>%
    summarise(cases = sum(cases), fmach = sum(factored_machines), .groups = "drop") %>%
    mutate(cpm = round(cases/fmach, 1))
  act_g <- act_b %>% filter(pm_cat != "Global") %>% group_by(pm) %>%
    summarise(cases = sum(cases), fmach = sum(fmach), .groups = "drop") %>%
    mutate(cpm = round(cases/fmach, 1), pm_cat = "Global")
  act <- bind_rows(act_b, act_g) %>%
    select(pm, pm_cat, cpm) %>% mutate(met = "Active", date = i)
  rm(w, ann_b, ann_g, act_sids, act_b, act_g)

  bind_rows(ann, act)
}) %>% bind_rows()
gc()
cat(sprintf("[%s] CPM History done.\n", format(Sys.time(),"%H:%M:%S")))
Sys.sleep(3)

# Goals ------------------------------------------------------------------------
cat(sprintf("[%s] Goals...\n", format(Sys.time(),"%H:%M:%S")))
gt_dates <- c(ttt, ttt - months(1), ttt - months(12))

goal_table_0.2 <- lapply(gt_dates, function(xxdate) {
  cc_global %>%
    filter(slsp("bundle|pending|demo", program),
           date >= xxdate - months(11), date <= xxdate, data == 1, !is.na(pm)) %>%
    group_by(store_id, pm, pm_cat) %>%
    summarise(program   = last(program),
              machines  = sum(machines[date == xxdate]),
              st_date   = first(st_date),
              ltm_cases = sum(cases),
              fmach     = sum(factored_machines),
              .groups   = "drop") %>%
    mutate(cpm = ltm_cases / fmach) %>%
    filter(slsp("bundle", program), machines > 0, st_date <= xxdate - months(2)) %>%
    group_by(pm, pm_cat) %>%
    summarise(`% Non-Purchasing`   = sum(cpm < 2,             na.rm = TRUE) / n(),
              `% Under Performing` = sum(cpm >= 2 & cpm < 8, na.rm = TRUE) / n(),
              .groups = "drop") %>%
    mutate(across(c(`% Non-Purchasing`,`% Under Performing`), ~percent(., accuracy = 0.1)),
           date_group = xxdate)
}) %>% bind_rows()

goal_table_0.1 <- lapply(gt_dates, function(xxdate) {
  cc_global %>%
    select(pm, pm_cat, store_id, date, chain, machines, cases, st_date, program, data) %>%
    arrange(store_id, pm_cat, date) %>%
    group_by(store_id, pm, pm_cat) %>%
    mutate(first_date = min(date)) %>%
    filter(any(machines > 0), any(slsp("bundle", program)),
           case_when(any(date == st_date) ~ any(program[date == st_date] == "BUNDLE"), TRUE ~ TRUE)) %>%
    left_join(last_purch_by_store, c("store_id" = "store_id_id")) %>%
    ungroup() %>%
    filter(case_when(is.na(lastpurch) ~ TRUE,
                     lastpurch < first_date ~ lastpurch < st_date - months(2),
                     TRUE ~ TRUE)) %>%
    group_by(store_id, pm, pm_cat) %>%
    filter(date >= st_date - months(2)) %>%
    mutate(ctq = cumsum(cases)) %>%
    ungroup() %>%
    mutate(nmno = interval(st_date, date) / months(1) + 1) %>%
    filter(nmno >= 2) %>%
    group_by(store_id, pm, pm_cat) %>%
    filter(ctq == 0 | lag(ctq) == 0, date >= xxdate - months(1)) %>%
    filter(any(slsp("bundle", program) & machines > 0),
           all(data[date >= st_date] == 1)) %>%
    ungroup() %>%
    filter(st_date <= date - months(1), nmno >= 2) %>%
    filter(date >= xxdate - months(1)) %>%
    group_by(pm, pm_cat, store_id) %>%
    filter(any(date == xxdate)) %>%
    mutate(is_nmno   = ctq <= 0 & machines > 0,
           nmno_bins = cut(nmno, c(2:12, Inf), 2:12, right = FALSE)) %>%
    reframe(
      ncat      = case_when(any(date == xxdate & nmno == 2 & is_nmno) ~ "New",
                            all(is_nmno) ~ "Stayed",
                            any(!is_nmno & date >= xxdate) ~ "Left"),
      nmno      = nmno[date == xxdate],
      nmno_bins = nmno_bins[date == xxdate],
      machines  = machines[date == xxdate]) %>%
    left_join(cpm_hist_data %>%
                ungroup() %>%
                group_modify(~{ .x %>% bind_rows(.x %>% mutate(pm_cat = "Global")) }) %>%
                group_by(pm, pm_cat) %>%
                summarise(t_elig = sum(elig)), c("pm","pm_cat")) %>%
    left_join(cc %>%
                group_modify(~{ .x %>% bind_rows(.x %>% mutate(pm_cat = "Global")) }) %>%
                arrange(store_id, pm_cat, desc(date)) %>%
                filter(slsp("bundle|demo", program), data == 1,
                       date <= xxdate, st_date <= xxdate - months(4)) %>%
                group_by(store_id, pm, pm_cat) %>%
                mutate(drought     = case_when(cases == 0 ~ consecutive_id(cases == 0)), .before = 1) %>%
                group_by(store_id, pm, pm_cat, drought) %>%
                mutate(drought_len = n(), .before = 1) %>%
                ungroup() %>%
                filter(drought == 1, date == xxdate) %>%
                group_by(pm, pm_cat) %>%
                summarise(n_drought_get_5 = sum(drought_len >= 5)), c("pm","pm_cat")) %>%
    filter(slsp("new|stayed", ncat)) %>%
    group_by(pm, pm_cat) %>%
    summarise(t_nmno = n(), mno4 = sum(nmno >= 4),
              p5mno  = (sum(nmno >= 5) + n_drought_get_5[1]) / t_elig[1]) %>%
    mutate(date_group = xxdate)
}) %>% bind_rows()

goal_table <- goal_table_0.1 %>%
  mutate(p5mno = percent(p5mno, 0.01)) %>%
  left_join(goal_table_0.2, c("pm","pm_cat","date_group")) %>%
  rename(`NMNO's` = t_nmno, `>4MNO` = mno4, `%5MNO` = p5mno) %>%
  mutate(date_group = case_when(date_group == gt_dates[1] ~ "Current",
                                date_group == gt_dates[2] ~ "Last Month",
                                date_group == gt_dates[3] ~ "Last Year"),
         across(everything(), as.character)) %>%
  pivot_longer(c(`% Non-Purchasing`,`NMNO's`,`>4MNO`,`%5MNO`,`% Under Performing`),
               names_to = "Metric", values_to = "vv") %>%
  pivot_wider(names_from = date_group, values_from = vv) %>%
  mutate(Metric = case_when(Metric == "% Non-Purchasing"   ~ "% CPM < 2",
                            Metric == "% Under Performing" ~ "% 2 \u2264 CPM < 8",
                            .default = Metric),
         Goal   = case_when(Metric == "% CPM < 2"              ~ "<2%",
                            Metric == "NMNO's"                  ~ "-",
                            Metric == ">4MNO"                   ~ "-",
                            Metric == "%5MNO"                   ~ "<5%",
                            Metric == "% 2 \u2264 CPM < 8"     ~ "<9%"),
         .before = Current)
gc()
cat(sprintf("[%s] Goals done.\n", format(Sys.time(),"%H:%M:%S")))
Sys.sleep(3)

# Line graph — sequential rollapply (was doParallel 10 cores) -----------------
cat(sprintf("[%s] Line graph (sequential rollapply)...\n", format(Sys.time(),"%H:%M:%S")))
line_graph_data1 <- cc_global %>%
  filter(slsp("bundle|demo|pending", program), data == 1) %>%
  group_by(store_id, pm, pm_cat) %>%
  arrange(date, .by_group = TRUE) %>%
  mutate(
    ltm_cases = data.table::frollsum(cases,             n = 12, align = "right", fill = NA, na.rm = TRUE),
    ltm_fmach = data.table::frollsum(factored_machines, n = 12, align = "right", fill = NA, na.rm = TRUE),
    cpm       = ltm_cases / ltm_fmach) %>%
  ungroup() %>%
  mutate(st_date  = floor_date(st_date, "month"),
         cpm_bins = cut(cpm, breaks = c(-Inf, seq(2,40,2), 50, 60, Inf),
                        labels = c(0, seq(2,40,2), 50, 60), right = FALSE)) %>%
  filter(cpm < 12, date <= ttt, date >= lttm,
         st_date <= date - months(2), slsp("bundle", program), machines > 0) %>%
  group_by(pm, pm_cat, date, cpm_bins) %>%
  summarise(n = n()) %>%
  ungroup()
gc()
cat(sprintf("[%s] Line graph done.\n", format(Sys.time(),"%H:%M:%S")))
Sys.sleep(2)

# Bintrav ----------------------------------------------------------------------
cat(sprintf("[%s] Bintrav...\n", format(Sys.time(),"%H:%M:%S")))
bintrav <- cc_global %>%
  select(store_id, dist, chain, date, st_date, data, program, mfac,
         machines, factored_machines, cases, pm, pm_cat) %>%
  arrange(store_id, pm_cat, date) %>%
  filter(slsp("bundle|pending|demo", program), data == 1) %>%
  group_by(store_id, pm, pm_cat) %>%
  arrange(date, .by_group = TRUE) %>%
  mutate(
    .rc = data.table::frollsum(cases,             n = 12, align = "right", fill = NA, na.rm = TRUE),
    .rf = data.table::frollsum(factored_machines, n = 12, align = "right", fill = NA, na.rm = TRUE),
    cpm = .rc / .rf) %>%
  select(-.rc, -.rf) %>%
  ungroup() %>%
  mutate(cpm_bins = cut(cpm, breaks = c(-Inf, seq(2,40,2), 50, 60, Inf),
                        labels = c(0, seq(2,40,2), 50, 60), right = FALSE)) %>%
  filter(st_date <= ttt - months(2), machines > 0,
         date == ttt | date == ttt - months(1),
         slsp("bundle", program)) %>%
  group_by(store_id, pm, pm_cat) %>%
  filter(any(date == ttt), any(cpm < 12)) %>%
  summarise(prev = first(cpm_bins), cur = last(cpm_bins)) %>%
  pivot_longer(prev:cur, names_to = "prd", values_to = "cpm_bins") %>%
  group_by(store_id, pm, pm_cat) %>%
  mutate(travel = case_when(all(cpm_bins == cpm_bins[1]) ~ "stay",
                            prd == "prev" ~ "out", prd == "cur" ~ "in")) %>%
  distinct(cpm_bins, .keep_all = TRUE) %>%
  ungroup() %>%
  filter(cpm_bins %in% c(0:10)) %>%
  count(pm, pm_cat, cpm_bins, travel) %>%
  mutate(n = case_when(travel == "out" ~ n * -1, TRUE ~ as.numeric(n)))
gc()
cat(sprintf("[%s] Bintrav done.\n", format(Sys.time(),"%H:%M:%S")))
Sys.sleep(2)

# Removals ---------------------------------------------------------------------
cat(sprintf("[%s] Removals...\n", format(Sys.time(),"%H:%M:%S")))
lj1 <- lj %>%
  filter(slsp("remov", job_type)) %>%
  inner_join(cj %>%
               filter(cancelled == 0) %>%
               select(close_date = install_date, lj_id = logistic_job_id), c("id" = "lj_id")) %>%
  select(lj_id = id, job_number, store_id, close_date, nmach = machine_count, removal_reason) %>%
  mutate(close_date = as.Date(close_date)) %>%
  filter(close_date >= lttm) %>%
  mutate(cdate = floor_date(close_date, "month")) %>%
  group_by(lj_id, job_number, store_id, cdate, removal_reason) %>%
  summarise(n_rvd = sum(nmach))

rmvd_cpm <- cc_global %>%
  filter(date <= ttt, date >= st_date, st_date <= ttt - months(2), date >= lttm - months(1)) %>%
  group_by(store_id, pm, pm_cat) %>%
  mutate(
    ltm_cases = rollapply(cases,             width = 12, FUN = sum, align = "right", partial = TRUE),
    ltm_fmach = rollapply(factored_machines,  width = 12, FUN = sum, align = "right", partial = TRUE),
    cpm       = ltm_cases / ltm_fmach) %>%
  ungroup() %>%
  filter(date >= ttt - months(1)) %>%
  select(pm, pm_cat, store_id, date, dist, chain, st_date, machines, cases,
         ltm_cases, ltm_fmach, cpm, program, data) %>%
  group_by(store_id, pm, pm_cat) %>%
  mutate(cpm = case_when(slsp("inactive", last(program)) ~ cpm[1], TRUE ~ cpm), .before = program) %>%
  ungroup() %>%
  filter(date == ttt) %>%
  inner_join(lj1, c("store_id","date" = "cdate")) %>%
  mutate(
    cpm_bins       = cut(cpm, breaks = c(-Inf,2,4,6,8,10,12,Inf), labels = seq(0,12,2), right = FALSE),
    removal_reason = recode(tolower(removal_reason),
      "exiting frozen beverage"          = "ditching\nFUB",
      "low cpm/remediation"              = "remediation",
      "change of ownership"              = "new\nowners",
      "switch to non-frazil distributor" = "non-FP\ndistributor",
      "customer requested/other"         = "other",
      "switch to competitor"             = "switch to\ncompetitor",
      "customer dissatisfaction"         = "customer\ndissatisfaction",
      "demo/tradeshow/non-pm removal"    = "demo/tradeshow/\nnon-pm removal",
      "machine reduction"                = "machine\nreduction"),
    .before = program)

rm_ts <- lj1 %>%
  inner_join(cc %>%
               group_modify(~{ .x %>% bind_rows(.x %>% mutate(pm_cat = "Global")) }) %>%
               filter(date == ttt, st_date <= ttt - months(2)) %>%
               select(pm, pm_cat, store_id), c("store_id")) %>%
  filter(cdate >= lttm) %>%
  group_by(pm, pm_cat, cdate) %>%
  summarise(n = sum(n_rvd))
gc()
cat(sprintf("[%s] Removals done.\n", format(Sys.time(),"%H:%M:%S")))
Sys.sleep(2)

# XMNO -------------------------------------------------------------------------
cat(sprintf("[%s] XMNO...\n", format(Sys.time(),"%H:%M:%S")))
xmno_df <- cc_global %>%
  left_join(last_purch_by_store %>% rename(store_id = store_id_id, lp = lastpurch),
            "store_id") %>%
  select(pm, pm_cat, store_id, chain, date, lp, mfac, machines, factored_machines,
         cases, st_date, program, data, pm_cat2) %>%
  filter(date <= ttt) %>%
  group_by(store_id, pm, pm_cat) %>%
  mutate(
    ltm_cases = rollapply(cases,             width = 12, FUN = sum, align = "right", partial = TRUE),
    ltm_fmach = rollapply(factored_machines,  width = 12, FUN = sum, align = "right", partial = TRUE),
    cpm       = ltm_cases / ltm_fmach, .before = program) %>%
  ungroup() %>%
  filter(data == 1, slsp("bundle", program), lp >= st_date - months(2), machines > 0) %>%
  arrange(store_id, pm, pm_cat, desc(date)) %>%
  group_by(store_id, pm, pm_cat) %>%
  mutate(run  = data.table::rleid(cases == 0)) %>%
  group_by(store_id, pm, pm_cat, run) %>%
  mutate(xmno = n(), fx = sum(mfac), fx_date1 = min(date), fx_date2 = max(date)) %>%
  ungroup() %>%
  filter(run == 1, cases == 0, date == ttt) %>%
  distinct(store_id, pm, pm_cat, .keep_all = TRUE) %>%
  rename(xmno_cat = pm_cat2)

xmno_pdata1 <- xmno_df %>%
  filter(xmno >= 6) %>%
  group_by(pm, pm_cat, xmno_cat) %>%
  summarise(machines = n()) %>%
  mutate(p = machines/sum(machines))

xmno_pdata2 <- xmno_df %>%
  mutate(cpm_bins = cut(cpm, breaks = c(-Inf,2,4,6,8,10,12,Inf),
                        labels = c(0,2,4,6,8,10,12), right = FALSE)) %>%
  filter(cpm_bins != 12) %>%
  group_by(pm, pm_cat, cpm_bins, xmno_cat) %>%
  summarise(machines = n()) %>%
  ungroup()

xmno_pdata3 <- xmno_df %>%
  filter(xmno <= 12) %>%
  mutate(xmno = cut(xmno, c(-Inf,3,5,6,8,10,12,Inf),
                    c("1-2","3-4","5","6-7","8-9","10-11","12"),
                    right = FALSE, include.lowest = TRUE)) %>%
  group_by(pm, pm_cat, xmno, xmno_cat) %>%
  summarise(machines = n()) %>%
  mutate(xmno_num = as.numeric(xmno), p = machines/sum(machines)) %>%
  ungroup()
gc()
cat(sprintf("[%s] XMNO done.\n", format(Sys.time(),"%H:%M:%S")))
Sys.sleep(2)

# Sleuthing --------------------------------------------------------------------
cat(sprintf("[%s] Sleuthing...\n", format(Sys.time(),"%H:%M:%S")))
sleuth_ws_plot_data <- cc_global %>%
  filter(date == tttc) %>%
  select(pm, pm_cat, store_id) %>%
  inner_join(machs_pm_cat %>%
               filter(!is.na(watch_status)) %>%
               select(store_id, watch_status), "store_id") %>%
  count(watch_status, pm, pm_cat) %>%
  group_by(pm, pm_cat, watch_status) %>%
  mutate(ws_n = sum(n)) %>%
  ungroup()

sleuth_pm_plot_data <- cc_global %>%
  filter(date == tttc) %>%
  select(pm, pm_cat, store_id) %>%
  inner_join(machs_pm_cat %>%
               filter(!is.na(watch_status)) %>%
               select(store_id, watch_status), "store_id") %>%
  count(pm, pm_cat, watch_status) %>%
  group_by(pm, pm_cat) %>%
  mutate(pws = n/sum(n)) %>%
  arrange(pm, -n) %>%
  mutate(watch_status = forcats::fct_reorder(watch_status, pws, .desc = TRUE))
gc()
cat(sprintf("[%s] Sleuthing done.\n", format(Sys.time(),"%H:%M:%S")))

# Exports ----------------------------------------------------------------------
cat(sprintf("[%s] Exports...\n", format(Sys.time(),"%H:%M:%S")))

sdeal1 <- sdeal %>%
  select(deal_id = id, store_id, deal_number, deal_note, type_id, hs_stage_id) %>%
  left_join(hspipe_stage %>% select(id, stage = label), join_by(hs_stage_id == id)) %>%
  inner_join(ljtype %>%
               filter(slsp("removal", name)) %>%
               select(type_id = id, deal_type = name), "type_id") %>%
  left_join(exstat %>%
              select(status_id = id, deal_id, status_date = date, deal_status = status), "deal_id") %>%
  mutate(deal_note = case_when(!slsp("^$", deal_note) ~
           gsub("<.*?>|&nbsp;", "", deal_note)) %>% trimws()) %>%
  filter(slsp("remov", deal_type),
         !slsp("^closed$|^job completed$|^cancelled$", stage)) %>%
  arrange(store_id, deal_id, status_date) %>%
  group_by(store_id) %>%
  filter(status_date == max(status_date), status_id == max(status_id)) %>%
  ungroup() %>%
  distinct(store_id, .keep_all = TRUE) %>%
  select(store_id, deal_id, deal_number, deal_type, deal_status, deal_note)

last_note <- rem %>%
  inner_join(atrisk %>% select(id, store_id = stores_id), join_by(atrisk_id == id)) %>%
  group_by(store_id) %>%
  filter(created_at == max(created_at)) %>%
  select(store_id, call_date, call_notes = notes)

# street/city/state/zip come from cpm_table join into cc already
# keep store_addr as fallback for any missing values
store_addr <- stores %>%
  select(store_id = id, street, city, state, zip)

store_nam <- stores %>%
  select(store_id = id, region_id) %>%
  left_join(sales_regions %>% select(region_id = id, NAM = name), "region_id") %>%
  select(store_id, NAM)

# export_base uses full cc (all dates) for rolling calculations, filter to ttt at end
# This matches original data_builder.R structure
export_base <- cc %>%
  arrange(store_id, date) %>%
  filter(pm != "total") %>%
  group_by(store_id, pm, pm_cat) %>%
  mutate(
    elig2         = slsp("bundle|pending|demo", program) & data == 1,
    ltm_cases     = rollapplyr(cases    * elig2, width = 12, FUN = sum, partial = TRUE),
    fmach         = rollapplyr(machines * mfac * elig2, width = 12, FUN = sum, partial = TRUE),
    cpm           = ltm_cases / fmach,
    last_month_cpm= lag(cpm),
    last_purch    = suppressWarnings(max(date[cases > 0], na.rm = TRUE)),
    np_run        = case_when(date > ttt ~ -1L, .default = consecutive_id(cases > 0)),
    last_sum      = sum(cases[np_run == max(np_run) & date >= st_date])) %>%
  group_by(store_id, pm, pm_cat, np_run) %>%
  mutate(months_no_purch = sum(date >= st_date & machines > 0)) %>%
  group_by(store_id, pm, pm_cat) %>%
  mutate(months_no_purch = case_when(
    last_sum > 0 ~ 0L,
    .default     = months_no_purch[np_run == max(np_run) & date >= st_date][1])) %>%
  ungroup() %>%
  filter(date == ttt) %>%
  left_join(store_addr, "store_id") %>%
  left_join(sdeal1,     "store_id") %>%
  left_join(last_note,  "store_id") %>%
  left_join(store_nam,  "store_id") %>%
  mutate(across(c(ltm_cases:last_month_cpm), ~replace(., !is.finite(.), 0)),
         last_purch = case_when(is.finite(last_purch) ~ last_purch))

export_base1 <- export_base %>%
  group_by(store_id) %>%
  filter(any(machines[date >= lttm] > 0), data == 1) %>%
  ungroup() %>%
  filter(slsp("bundle|inactive", program) |
           (slsp("pending", program) & machines > 0)) %>%
  left_join(bind_rows(
    nmno_df %>% filter(slsp("new|stay", ncat)) %>%
      distinct(store_id) %>% mutate(`PM Category` = "NMNO"),
    xmno_df %>% filter(xmno >= 3) %>%
      distinct(store_id) %>% mutate(`PM Category` = "3MNO")
  ) %>% distinct(store_id, .keep_all = TRUE), "store_id") %>%
  select(`Store ID`                  = store_id,
         `External Store ID`         = external_store_id,
         `Store Name`                = store_name,
         Address                     = street,
         City                        = city,
         State                       = state,
         Zip                         = zip,
         Date                        = date,
         Distributor                 = dist,
         `Chain Name`                = chain,
         Label                       = pm_cat,
         `Label 2`                   = pm_cat2,
         CPM                         = cpm,
         `Last Month CPM`            = last_month_cpm,
         `# of Machines`             = machines,
         `LTM Cases`                 = ltm_cases,
         `Date of Last Purchase`     = last_purch,
         `Months w/o Purchase`       = months_no_purch,
         `Current Account Start Date`= st_date,
         `Deal Number`               = deal_number,
         `Deal Type`                 = deal_type,
         `Deal Status`               = deal_status,
         `Deal Note`                 = deal_note,
         PM                          = pm,
         `Date of Last Note`         = call_date,
         `Last Note`                 = call_notes,
         program,
         `PM Category`,
         NAM) %>%
  mutate(across(where(is.numeric), ~round(., 1)))

export_nmno <- nmno_df %>%
  filter(pm != "total", pm_cat != "Global") %>%
  mutate(ncat = case_match(ncat, "Left" ~ "Out", "Stayed" ~ "Stay", "New" ~ "In")) %>%
  select(store_id, `NMNO Label` = ncat, NMNO = nmno, `NMNO Bin` = nmno_bins) %>%
  left_join(export_base1 %>%
              select(-`Last Month CPM`) %>%
              filter(Date == ttt), c("store_id" = "Store ID")) %>%
  relocate(`NMNO Label`:`NMNO Bin`, .after = last_col()) %>%
  rename(`Store ID` = store_id) %>%
  relocate(`External Store ID`, .before = 2) %>%
  relocate(Date, Distributor, `Chain Name`, .after = Zip)

export_removals <- rmvd_cpm %>%
  filter(pm != "total", pm_cat != "Global") %>%
  select(lj_id, `Job #` = job_number, store_id, removal_reason, n_rvd) %>%
  mutate(removal_reason = gsub("\n", " ", removal_reason)) %>%
  left_join(export_base %>%
              left_join(store_addr, "store_id") %>%
              select(`Store ID` = store_id, `External Store ID` = external_store_id,
                     `Store Name` = store_name, Address = street, City = city,
                     State = state, Zip = zip, Date = date, Distributor = dist,
                     `Chain Name` = chain, Label = pm_cat, CPM = cpm,
                     `Last Month CPM` = last_month_cpm, `# of Machines` = machines,
                     `LTM Cases` = ltm_cases, `Date of Last Purchase` = last_purch,
                     `Months w/o Purchase` = months_no_purch,
                     `Current Account Start Date` = st_date, PM = pm,
                     `Date of Last Note` = call_date, `Last Note` = call_notes) %>%
              mutate(across(where(is.numeric), ~round(., 1))) %>%
              select(-c(`Last Month CPM`,`# of Machines`)) %>%
              filter(Date == ttt), c("store_id" = "Store ID")) %>%
  left_join(joblog, c("lj_id" = "job_id")) %>%
  rowwise() %>%
  mutate(details       = replace(details, is.na(details), '{"serial_number": ""}'),
         serial_number = tryCatch(jsonlite::fromJSON(details)$serial_number, error = function(e) ""),
         .before       = 1) %>%
  tidyr::separate_longer_delim(serial_number, regex(", ?")) %>%
  ungroup() %>%
  select(-lj_id) %>%
  rename(`Serial Number` = serial_number, `Store ID` = store_id,
         `Removal Reason` = removal_reason, `# Machines Removed` = n_rvd) %>%
  relocate(`External Store ID`, .before = 2) %>%
  relocate(Date, Distributor, `Chain Name`, .after = Zip) %>%
  left_join(store_nam, join_by(`Store ID` == store_id))

export_sleuth <- export_base1 %>%
  filter(Date == tttc) %>%
  inner_join(machs_pm_cat %>%
               filter(!is.na(watch_status)) %>%
               select(store_id, machine_id, watch_status),
             c("Store ID" = "store_id")) %>%
  left_join(machs %>% select(machine_id = id, serial_number = bunn_serial_number), "machine_id") %>%
  select(-machine_id) %>%
  relocate(serial_number) %>%
  rename(`Serial Number` = serial_number) %>%
  relocate(`External Store ID`, .before = 2) %>%
  relocate(Date, Distributor, `Chain Name`, .after = Zip)

gc()
cat(sprintf("[%s] Exports done.\n", format(Sys.time(),"%H:%M:%S")))

# =============================================================================
# Write cache
# =============================================================================
cat(sprintf("[%s] Writing cache...\n", format(Sys.time(),"%H:%M:%S")))

pm_names <- sort(unique(c(as.character(nmno_df$pm),
                           as.character(t_ann_cpm$pm))))

payload <- list(
  ttt           = format(ttt,  "%Y-%m-%d"),
  lttm          = format(lttm, "%Y-%m-%d"),
  pm_names      = pm_names,
  pm_cat_names  = c("Global","Addressable","Top 202"),
  goal_table    = goal_table,
  cpm_hist_data = cpm_hist_data1,
  cpm_history   = cpm_history,
  t_ann_cpm     = t_ann_cpm,
  t_active_cpm  = t_active_cpm,
  line_graph    = line_graph_data1,
  bintrav       = bintrav,
  nmno          = nmno_df,
  xmno_pdata1   = xmno_pdata1,
  xmno_pdata2   = xmno_pdata2,
  rm_ts         = rm_ts,
  removal_count = rmvd_cpm %>% count(removal_reason, wt = n_rvd, name = "n"),
  removal_cpm   = list(),
  sleuth_ws     = sleuth_ws_plot_data,
  sleuth_pm     = sleuth_pm_plot_data,
  export_base   = export_base1,
  export_nmno   = export_nmno,
  export_removals = export_removals,
  export_sleuth = export_sleuth
)

write(toJSON(payload, auto_unbox = TRUE, na = "null", digits = 4),
      "/opt/pm/dashboard_cache.json")

cat(sprintf("[%s] === DONE. Written to /opt/pm/dashboard_cache.json ===\n",
            format(Sys.time(),"%H:%M:%S")))
