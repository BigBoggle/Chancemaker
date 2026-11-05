// TODO: Upon clicking each th,the table should sort by that category, be it alphabetically or by some numeric order

function sortTableByColumn(table, columnIndex, type) {
  const tbody = table.querySelector("tbody");
  const rows = Array.from(tbody.querySelectorAll("tr"));

  // Determine sorting direction
  const currentDirection = table.dataset.sortDirection || "asc";
  const currentColumn = table.dataset.sortColumn;

  const direction =
    currentColumn == columnIndex && currentDirection === "asc" ? "desc" : "asc";

  rows.sort((a, b) => {
    let valA = a.cells[columnIndex].textContent.trim();
    let valB = b.cells[columnIndex].textContent.trim();

    if (type === "number") {
      valA = parseFloat(valA.replace(/[^\d.-]/g, ""));
      valB = parseFloat(valB.replace(/[^\d.-]/g, ""));

      valA = Number.isNaN(valA) ? -Infinity : valA;
      valB = Number.isNaN(valB) ? -Infinity : valB;
    } else if (type === "odds") {
      // FIXME: Sorting by odds doesn't work
      const parseOdds = (value) => {
        const parts = value.split("-").map(Number);
        return parts.length === 2 && parts[1] !== 0
          ? parts[0] / parts[1]
          : -Infinity;
      };

      valA = parseOdds(valA);
      valB = parseOdds(valB);
    } else {
      valA = valA.toLowerCase();
      valB = valB.toLowerCase();
    }

    if (valA < valB) return direction === "asc" ? -1 : 1;
    if (valA > valB) return direction === "asc" ? 1 : -1;

    return 0;
  });

  rows.forEach((row) => tbody.appendChild(row));

  table.dataset.sortColumn = columnIndex;
  table.dataset.sortDirection = direction;

  const headers = table.querySelectorAll("th");
  headers.forEach((header, index) => {
    header.classList.remove("sorted-column");

    if (index === columnIndex) {
      header.classList.add("sorted-column");

      // Show sorting direction
      header.dataset.direction = direction;
    } else {
      delete header.dataset.direction;
    }
  });
}

// Fetch Races fron JSON Endpoint and return the data as an HTML table
async function loadRaces() {
  try {
    //Try so in case it fails it automatically umps to catch\
    const response = await fetch("/races"); // Fetches our JSON from localhost:5000/races
    if (!response.ok) {
      throw new Error(`HTTP error: ${response.status}`);
    } else {
      console.log("Response status:", response.status);
    }
    const races = await response.json(); // Wait for the response JSON fetched from /races

    console.log("Races received:", races);
    console.log("Number of races:", races.length);

    const container = document.getElementById("raceContainer");
    container.innerHTML = ""; // Initialized initially blank contents of container

    races.forEach((race) => {
      // Create card container
      const card = document.createElement("div");
      card.className = "race-card";

      // Format winning prediction safely
      let rowsHtml = "";
      race.entries.forEach((entry) => {
        const pred = entry.predictions[0] || {};
        const isWinner = pred.is_winner ? "winner" : "";
        const winProb =
          pred.win_probability != null
            ? (pred.win_probability * 100).toFixed(1) + "%"
            : "N/A";

        rowsHtml += `
                          <tr class="${isWinner}">
                              <td>${entry.program_number}</td>
                              <td>${entry.horse_name}</td>
                              <td>${entry.horse_trainer}</td>
                              <td>${entry.jockey_name}</td>
                              <td>${entry.running_style}</td>
                              <td>${entry.morning_line_odds}</td>
                              <td>${entry.speed_figures}</td>
                              <td>#${pred.rank || "N/A"}</td>
                              <td>${winProb}</td>
                          </tr>                        `;
      });

      // Set the inner structure of the race card
      card.innerHTML = `
                      <h2>${race.race_name}</h2>
                      <p><strong>Track:</strong> ${race.track_name} (${race.track_location}) | <strong>Date:</strong> ${race.race_date} | <strong>Distance:</strong> ${race.race_distance_f}f</p>
                      <table>
                          <thead>
                              <tr>
                                <th onclick="sortTableByColumn(this.closest('table'), 0, 'number')">Program #</th>
                                <th onclick="sortTableByColumn(this.closest('table'), 1, 'text')">Horse</th>
                                <th onclick="sortTableByColumn(this.closest('table'), 2, 'text')">Trainer</th>
                                <th onclick="sortTableByColumn(this.closest('table'), 3, 'text')">Jockey</th>
                                <th onclick="sortTableByColumn(this.closest('table'), 4, 'text')">Style</th>
                                <th onclick="sortTableByColumn(this.closest('table'), 5, 'odds')">Odds</th>
                                <th onclick="sortTableByColumn(this.closest('table'), 6, 'number')">Speed</th>
                                <th onclick="sortTableByColumn(this.closest('table'), 7, 'number')">Rank</th>
                                <th onclick="sortTableByColumn(this.closest('table'), 8, 'number')">Probability</th>
                              </tr>
                          </thead>
                          <tbody>
                              ${rowsHtml}
                          </tbody>
                      </table>
                  `;

      container.appendChild(card);
      const table = card.querySelector("table");
      sortTableByColumn(table, 0, "number");
    });
  } catch (error) {
    console.error("Error:", error);
    document.getElementById("raceContainer").innerHTML =
      '<p style="color: red;">Failed to load race data.</p>';
  }
}

document.addEventListener("DOMContentLoaded", function () {
  loadRaces();
});
