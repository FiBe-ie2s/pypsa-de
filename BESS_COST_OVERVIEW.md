# Battery Energy Storage System (BESS) Cost Assumptions Overview
## PyPSA-DE Repository

---

## Summary

The pypsa-de repository uses comprehensive battery storage cost assumptions that distinguish between:
1. **Grid-scale lithium-ion battery (Li-ion)** - Primary BESS technology
2. **Home batteries** - Distributed residential storage
3. **Alternative long-duration technologies** - LFP, Vanadium, Liquid-Air (LAIR), Compressed-Air (PAIR), Iron-Air

---

## 1. PRIMARY DATA SOURCE

### Location
**File:** `data/costs/primary/v0.14.0-2020-v2/costs_YYYY.csv`

Where `YYYY` refers to the planning horizon year:
- `costs_2020.csv`
- `costs_2025.csv`
- `costs_2030.csv`
- `costs_2035.csv` (currently used in analysis.ipynb)
- `costs_2040.csv`
- `costs_2045.csv`
- `costs_2050.csv`

### Source Data Attribution

The cost data originates from:
- **Danish Energy Agency** - Technology Data Catalogue for Energy Storage (2020)
- Reference file: `inputs/technology_data_catalogue_for_energy_storage.xlsx`
- Currency year: **2020**

---

## 2. GRID-SCALE BATTERY STORAGE COSTS (2035 Planning Horizon)

### 2.1 Battery Storage (Energy Component)

| Parameter | Value | Unit | Notes |
|-----------|-------|------|-------|
| **Investment Cost** | 125.48 | EUR/kWh | Energy storage expansion cost |
| **Lifetime** | 27.5 | years | Technical lifetime |
| **Source** | Danish Energy Agency | - | Technology Data Catalogue for Energy Storage |

### 2.2 Battery Inverter (Power Component)

| Parameter | Value | Unit | Notes |
|-----------|-------|------|-------|
| **Investment Cost** | 138.24 | EUR/kW | Output capacity expansion cost |
| **FOM (Fixed O&M)** | 0.4154 | %/year | Fixed operational and maintenance costs |
| **Efficiency** | 0.96 | per unit | Round-trip efficiency (DC) |
| **Lifetime** | 10.0 | years | Technical lifetime (replacement required ~2.7 times over battery storage lifetime) |
| **Source** | Danish Energy Agency | - | Technology Data Catalogue for Energy Storage |

### 2.3 Marginal Operating Costs

| Component | Value | Unit | Notes |
|-----------|-------|------|-------|
| **Battery Storage** | 0 | EUR/MWh | From custom_costs.csv (prevents mathematical degeneracy) |
| **Battery Inverter** | 0 | EUR/MWh | From custom_costs.csv (prevents mathematical degeneracy) |

---

## 3. RESIDENTIAL BATTERY STORAGE COSTS (Home Batteries, 2035)

Home batteries are higher-cost due to distributed nature and residential integration.

### 3.1 Home Battery Storage (Energy Component)

| Parameter | Value | Unit | Notes |
|-----------|-------|------|-------|
| **Investment Cost** | 180.44 | EUR/kWh | Energy storage expansion cost |
| **Lifetime** | 27.5 | years | Technical lifetime |
| **Source** | Global Energy System (Energywatch/LTU) + Danish Energy Agency | - | Technology Data Catalogue for Energy Storage |

**Cost Premium:** Home batteries cost **1.44× more per kWh** than grid-scale BESS

### 3.2 Home Battery Inverter (Power Component)

| Parameter | Value | Unit | Notes |
|-----------|-------|------|-------|
| **Investment Cost** | 198.40 | EUR/kW | Output capacity expansion cost |
| **FOM (Fixed O&M)** | 0.4154 | %/year | Fixed operational and maintenance costs |
| **Efficiency** | 0.96 | per unit | Round-trip efficiency (DC) |
| **Lifetime** | 10.0 | years | Technical lifetime |
| **Source** | Energywatch/LTU + Danish Energy Agency | - | Technology Data Catalogue for Energy Storage |

**Cost Premium:** Home battery inverters cost **1.44× more per kW** than grid-scale inverters

---

## 4. CONFIGURATION & OPERATIONAL PARAMETERS

### 4.1 Battery Duration (Max Hours)

Configured in `config/config.default.yaml`, line 127:

```yaml
electricity:
  max_hours:
    battery: 6
    "li-ion": 6
    lfp: 6
    vanadium: 10
    lair: 12
    pair: 24
    "iron-air": 100
    H2: 168
```

- **Grid-scale BESS maximum hours:** **6 hours**
- This means: Maximum energy capacity = 6 × rated power capacity
- E.g., a 1 GW battery can store up to 6 GWh of energy
- **Rationale:** Li-ion batteries are optimized for short-to-medium duration storage (minutes to hours)

### 4.2 Extendable Technologies

Configuration in `config/config.default.yaml`, line 147:

```yaml
extendable_carriers:
  Store:
    - battery
    - H2
```

**Battery storage is marked as extendable**, meaning the optimization model can expand installed capacities beyond existing levels.

### 4.3 Capacity Estimation

Setting in `config/config.default.yaml`, line 181:

```yaml
estimate_battery_capacities: true
```

When enabled, the model estimates existing battery capacities based on PowerPlant Matching database or other sources.

---

## 5. BESS IMPLEMENTATION IN NETWORK MODEL

### 5.1 Dual-Bus Architecture

Grid-scale battery storage is implemented using:
- **Bus 0:** AC power bus (grid connection)
- **Battery Bus:** Dedicated battery carrier bus
- **Two Links:** 
  - Battery Charger: AC bus → Battery bus (with charging efficiency)
  - Battery Discharger: Battery bus → AC bus (with discharging efficiency)
- **Store:** Energy storage on the battery bus

### 5.2 Cost Component Mapping

The total annualized cost consists of:

1. **Storage Cost** (energy-based investment):
   - Formula: `capital_cost = investment × annuity_factor / max_hours / 1000`
   - Units: EUR/MWh (annual)

2. **Inverter Cost** (power-based investment):
   - Formula: `capital_cost = investment × annuity_factor / 1000`
   - Units: EUR/MW (annual)

3. **Fixed Operating Costs** (power-based FOM):
   - Formula: `FOM × capital_cost` (calculated annually)

---

## 6. ALTERNATIVE STORAGE TECHNOLOGIES

For comparison, the model includes cost data for alternative long-duration storage:

### 6.1 Lithium-Ion LFP (Longer Duration Variant)

- **Store Investment:** 236,482 EUR/MWh
- **Bicharger Investment:** 81,553 EUR/MW
- **Lifetime:** 16 years
- **Max Hours (config):** 6 hours

### 6.2 Vanadium Redox Flow (VRF)

- **Max Hours (config):** 10 hours
- **Technology Type:** Electrochemical (longer discharge times possible)
- **Cost Structure:** Similar component separation (charger, discharger, store)

### 6.3 Liquid Air (LAIR)

- **Store Investment:** 159,004 EUR/MWh
- **Charger Investment:** 475,721 EUR/MW
- **Discharger Investment:** 334,017 EUR/MW
- **Max Hours (config):** 12 hours
- **Technology Type:** Thermal storage

### 6.4 Compressed-Air Adiabatic (PAIR)

- **Store Investment:** 5,448 EUR/MWh (cavern storage - lowest energy cost)
- **Bicharger Investment:** 946,180 EUR/MW
- **Max Hours (config):** 24 hours
- **Technology Type:** Mechanical storage

### 6.5 Iron-Air Battery

- **Store Investment:** 12.64 EUR/kWh (lowest energy cost among electrochemical)
- **FOM:** 1.11 %/year
- **Lifetime:** 17.5 years
- **Max Hours (config):** 100 hours
- **Source:** Form Energy (2023)

---

## 7. COST PROCESSING WORKFLOW

### 7.1 Data Loading & Processing

**Script:** `scripts/process_cost_data.py`

Process flow:
1. Load base costs from `costs_YYYY.csv`
2. Apply custom cost modifications from `data/custom_costs.csv`
3. Calculate annualized `capital_cost` from investment + FOM
4. Link storage energy and power costs via `max_hours` parameter
5. Generate processed costs file: `resources/costs_YYYY_processed.csv`

### 7.2 Network Integration

**Script:** `scripts/add_electricity.py`

```python
STORE_LOOKUP = {
    "battery": {
        "store": "battery storage",
        "bicharger": "battery inverter",
        "roundtrip_correction": 0.5,  # AC-AC efficiency = 0.96^0.5
    },
    ...
}
```

The lookup table maps:
- Storage technology → Cost parameters
- Charger/discharger efficiency → Power loss calculation

---

## 8. CUSTOM COST MODIFICATIONS

**File:** `data/custom_costs.csv`

Marginal cost adjustments applied to prevent numerical optimization issues:

```csv
planning_horizon,technology,parameter,value,unit,source
all,battery,marginal_cost,0,EUR/MWh,Default value to prevent mathematical degeneracy
all,battery inverter,marginal_cost,0,EUR/MWh,Default value to prevent mathematical degeneracy
```

---

## 9. COST TRAJECTORY ACROSS PLANNING HORIZONS

Cost assumptions decline from 2020 to 2050 to reflect:
- Technological learning curves
- Economies of scale
- Industry maturation
- Manufacturing capacity growth

**Example:** Battery storage costs typically show 5-15% decline per decade in the model.

---

## 10. CONFIGURATION FILE REFERENCES

### Main Configuration Files

1. **`config/config.default.yaml`** (lines 116-181)
   - `max_hours` parameter (line 127)
   - `extendable_carriers` definition (line 147)
   - `estimate_battery_capacities` setting (line 181)

2. **`config/config.de.yaml`**
   - German-specific overrides

3. **`config/plotting.default.yaml`**
   - Visualization colors for battery technologies

---

## 11. VALIDATION & DIAGNOSTICS

From the analysis notebook (`thesis/analysis.ipynb`), battery summary statistics are displayed:

```
╔════════════════════════════════════════════════════════════════╗
║               Battery Storage Summary — DE 2035               ║
╠════════════════════════════════════════════════════════════════╣
║ 1) Grid-scale BESS (AC bus)
║     Existing:     Expansion:      Total:
║   Energy [GWh]    XXXX.XXX  →     XXXX.XXX
║   Charge [GW]     XXXX.XXX  →     XXXX.XXX
║   Discharge [GW]  XXXX.XXX  →     XXXX.XXX
║
║ 2) Home batteries (LV bus)
║   Similar structure...
║
║ 3) EV batteries (exogenous)
║   Similar structure...
```

---

## 12. KEY TAKEAWAYS

| Aspect | Details |
|--------|---------|
| **Primary Technology** | Lithium-ion (Li-ion) batteries |
| **Energy Cost (2035)** | 125.48 EUR/kWh |
| **Power Cost (2035)** | 138.24 EUR/kW |
| **Duration** | 6 hours (configurable) |
| **Data Vintage** | 2020 (Danish Energy Agency) |
| **Lifetime** | Storage: 27.5 yr; Inverter: 10 yr |
| **Efficiency** | 96% DC round-trip |
| **Home Battery Premium** | +44% on both energy and power costs |
| **Geographic Scope** | Germany (DE) + 32 EU countries |
| **Currency** | EUR (2020 prices) |

---

## 13. RELATED FILES & SCRIPTS

| File | Purpose |
|------|---------|
| `data/costs/primary/v0.14.0-2020-v2/costs_*.csv` | Base cost assumptions by year |
| `data/custom_costs.csv` | Custom cost modifications |
| `scripts/process_cost_data.py` | Cost data loading & processing |
| `scripts/add_electricity.py` | Network integration & component definitions |
| `scripts/prepare_sector_network.py` | Sector coupling & home battery setup |
| `config/config.default.yaml` | Global configuration (max_hours, extendable_carriers) |
| `thesis/analysis.ipynb` | Analysis & diagnostics (currently loaded with 2035 data) |

---

## 14. REFERENCES & ATTRIBUTION

- **Danish Energy Agency** (2020): Technology Data for Energy Storage
  - URL: https://ens.dk/en/our-services/projections-and-analyses/technology-data
  
- **Global Energy System based on 100% Renewable Energy** (2019)
  - Organization: Energywatch Group & Lund University
  - Primarily for home battery cost assumptions

- **Form Energy** (2023): Europe Modeling Recommendations for Iron-Air Batteries
  - Document: `docu/FormEnergy_Europe_modeling_recommendations_2023.03.pdf`

---

**Document Generated:** June 2026  
**Repository Version:** v2026.02.0  
**Analysis Period:** 2035 Planning Horizon (default in analysis.ipynb)
